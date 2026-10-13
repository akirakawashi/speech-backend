"""Контракт приёма: DTO, служебный доступ, актуальность и подтверждение после commit."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from src.application.dto.transcription_delivery import (
    ReceivedReply,
    ReceivedTranscription,
    ReceivingCall,
    StoredTranscriptionReceipt,
    TranscriptionClaim,
    TranscriptionDeliveryRequest,
    TranscriptionLease,
    TranscriptionSource,
)
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.transcription_delivery import TranscriptionDeliveryService
from src.di.http import create_app
from src.domain.calls.contracts import AudioStatus, JobKind, JobLane, JobStatus, ProcessingStatus
from src.presentation.http.transcription_results import transcription_delivery_dep
from src.setting.config import Settings

NOW = datetime(2026, 10, 13, 10, tzinfo=UTC)
TOKEN = "test-worker-key"
AUTH = "Bearer " + TOKEN
PRIVATE_TEXT = "Исходная реплика, которую нельзя возвращать в диагностике"


@pytest.fixture
def command() -> TranscriptionDeliveryRequest:
    """Вернуть доставку одной реплики неизвестного участника от Whisper."""
    return TranscriptionDeliveryRequest(
        claim=TranscriptionClaim(
            call_id=uuid4(), job_id=uuid4(), claim_token=uuid4(), worker_id="gpu-worker", attempt=1
        ),
        source=TranscriptionSource(object_key="calls/original.wav", channels=1),
        result=ReceivedTranscription(
            engine="whisper",
            model_name="large-v3",
            language="ru",
            role_method="none",
            duration_s=2.0,
            replies=(
                ReceivedReply(seq=0, speaker="unknown", start_ms=0, end_ms=1200, text=PRIVATE_TEXT),
            ),
        ),
    )


class Repository:
    """Подставной порт с явным commit/rollback; записи проверяются отдельно на PostgreSQL."""

    def __init__(self, command):
        self.events = []
        self.commit_error = None
        self.transaction_port = AsyncMock()
        tx = self.transaction_port
        tx.lock_call.return_value = ReceivingCall(
            id=command.claim.call_id,
            audio_status=AudioStatus.STORED,
            audio_object_key=command.source.object_key,
            audio_channels=command.source.channels,
            processing_status=ProcessingStatus.TRANSCRIBING,
        )
        tx.lock_jobs.return_value = (
            TranscriptionLease(
                id=command.claim.job_id,
                call_id=command.claim.call_id,
                kind=JobKind.TRANSCRIBE,
                status=JobStatus.RUNNING,
                lane=JobLane.BACKFILL,
                priority=100,
                attempts=1,
                locked_by=command.claim.worker_id,
                claim_token=command.claim.claim_token,
                lease_until=NOW + timedelta(minutes=15),
            ),
        )
        tx.receipt.return_value = None
        tx.database_time.return_value = NOW
        tx.analysis_attempts.return_value = 7
        tx.save_transcription.return_value = uuid4()
        tx.enqueue_analysis.return_value = uuid4()
        tx.complete.return_value = True

    @asynccontextmanager
    async def transaction(self):
        """Отдать порт и подтвердить commit только после успешного выполнения сценария."""
        self.events.append("begin")
        try:
            yield self.transaction_port
            if self.commit_error:
                raise self.commit_error
            self.events.append("commit")
        except BaseException:
            self.events.append("rollback")
            raise


async def test_receipt_follows_commit_and_analysis_uses_backend_policy(command) -> None:
    """Подтверждение следует после commit; очередь наследует срочность и лимит бэкенда."""
    repository = Repository(command)
    service = TranscriptionDeliveryService(repository, worker_token=TOKEN)
    receipt = await service.receive_authorized(command, AUTH)
    assert repository.events == ["begin", "commit"]
    assert receipt.claim_token == command.claim.claim_token
    job = repository.transaction_port.enqueue_analysis.call_args.args[0]
    assert (job.lane, job.priority, job.max_attempts) == (JobLane.BACKFILL, 100, 7)
    assert job.kind is JobKind.ANALYZE
    stored = repository.transaction_port.complete.call_args.args[1]
    assert isinstance(stored, StoredTranscriptionReceipt)
    assert stored.receipt == receipt and PRIVATE_TEXT not in stored.model_dump_json()


async def test_failed_commit_never_returns_receipt(command) -> None:
    """Успешные операции внутри транзакции не означают успешного подтверждения."""
    repository = Repository(command)
    repository.commit_error = RuntimeError("test commit failure")
    with pytest.raises(RuntimeError, match="test commit failure"):
        await TranscriptionDeliveryService(repository, worker_token=TOKEN).receive_authorized(
            command, AUTH
        )
    assert repository.events == ["begin", "rollback"]


async def test_final_lease_check_failure_rolls_back(command) -> None:
    """Потеря аренды на финальной проверке не подтверждает предварительные записи."""
    repository = Repository(command)
    repository.transaction_port.complete.return_value = False
    with pytest.raises(ApplicationError) as failure:
        await TranscriptionDeliveryService(repository, worker_token=TOKEN).receive_authorized(
            command, AUTH
        )
    assert failure.value.kind is ErrorKind.CONFLICT
    assert repository.events[-1] == "rollback"


@pytest.mark.parametrize(
    "field,value", [("claim_token", uuid4()), ("worker_id", "other"), ("attempt", 2)]
)
async def test_stale_claim_is_rejected_before_writes(command, field, value) -> None:
    """Чужой захват не проходит даже при совпадающем идентификаторе задания."""
    repository = Repository(command)
    changed = command.model_copy(update={"claim": command.claim.model_copy(update={field: value})})
    with pytest.raises(ApplicationError) as failure:
        await TranscriptionDeliveryService(repository, worker_token=TOKEN).receive_authorized(
            changed, AUTH
        )
    assert failure.value.kind is ErrorKind.CONFLICT
    repository.transaction_port.save_transcription.assert_not_awaited()


@pytest.mark.parametrize(
    "settings_token,authorization,status",
    [("", None, 503), (TOKEN, None, 401), (TOKEN, "Bearer wrong", 401)],
)
async def test_http_auth_before_database_creation(
    command, settings_token, authorization, status
) -> None:
    """Без ключа или с чужим ключом отказ приходит даже при отсутствии адреса базы."""
    app = create_app(
        Settings(
            _env_file=None,
            app_env="local",
            speech_auth_mode="open",
            speech_worker_api_token=settings_token,
        )
    )
    assert not any(path.startswith("/internal/") for path in app.openapi()["paths"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    ) as client:
        response = await client.post(
            "/internal/v1/transcription-results",
            json=command.model_dump(mode="json"),
            headers={"Authorization": authorization} if authorization else {},
        )
    assert response.status_code == status
    assert PRIVATE_TEXT not in response.text


async def test_http_roundtrip_and_validation_do_not_echo_text(command) -> None:
    """Роут отдаёт квитанцию; неправильный DTO отклоняется без исходных значений."""
    repository = Repository(command)
    service = TranscriptionDeliveryService(repository, worker_token=TOKEN)
    app = create_app(Settings(_env_file=None, app_env="local", speech_auth_mode="open"))
    app.dependency_overrides[transcription_delivery_dep] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    ) as client:
        response = await client.post(
            "/internal/v1/transcription-results",
            json=command.model_dump(mode="json"),
            headers={"Authorization": AUTH},
        )
        assert response.status_code == 200 and response.json()["claim_token"] == str(
            command.claim.claim_token
        )
        body = command.model_dump(mode="json")
        body["result"]["replies"][0]["end_ms"] = -1
        response = await client.post(
            "/internal/v1/transcription-results", json=body, headers={"Authorization": AUTH}
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid transcription result"}
        assert PRIVATE_TEXT not in response.text


@pytest.mark.parametrize(
    "update",
    [
        {"duration_s": float("nan")},
        {"duration_s": float("inf")},
        {"role_method": "llm_attribution"},
        {"summary": PRIVATE_TEXT},
    ],
)
def test_result_rejects_non_asr_or_unstorable_data(command, update) -> None:
    """Результат распознавания не принимает анализ, LLM-роли или бесконечную длительность."""
    with pytest.raises(ValidationError):
        ReceivedTranscription.model_validate({**command.result.model_dump(), **update})


def test_result_rejects_duplicate_sequence_and_unknown_contract(command) -> None:
    """Повтор номера реплики и незнакомая версия отсекаются до обращения к базе."""
    with pytest.raises(ValidationError):
        ReceivedTranscription.model_validate(
            {**command.result.model_dump(), "replies": [command.result.replies[0]] * 2}
        )
    with pytest.raises(ValidationError):
        TranscriptionDeliveryRequest.model_validate(
            {**command.model_dump(), "contract_version": "2.0"}
        )
