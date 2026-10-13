"""PostgreSQL: квитанция, защита захвата, замена входа и атомарность приёма."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from src.application.dto.transcription_delivery import (
    ReceivedReply,
    ReceivedTranscription,
    TranscriptionClaim,
    TranscriptionDeliveryRequest,
    TranscriptionReceipt,
    TranscriptionSource,
)
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.queue import CLAIM_TOKEN_KEY, TRANSCRIPTION_RECEIPT_KEY, NewJob
from src.application.transcription_delivery import TranscriptionDeliveryService
from src.di.http import create_app
from src.domain.calls.contracts import JobKind, JobLane
from src.infrastructure.persistence.reprocessing import reprocess
from src.infrastructure.persistence.transcription_delivery import (
    PostgresTranscriptionReceptionRepository,
    PostgresTranscriptionReceptionTransaction,
)
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.presentation.http.transcription_results import transcription_delivery_dep
from src.setting.config import Settings
from tests.speech_db_support import speech_test_database_url
from tests.test_speech_quality_db import _call, sessions as sessions

pytestmark = pytest.mark.skipif(
    not speech_test_database_url(), reason="SPEECH_POSTGRES_TEST_URL is required"
)
TOKEN = "test-worker-key"
AUTH = "Bearer " + TOKEN
WORKER = "gpu-test"
NEW_TEXT = "Новая расшифровка для приёма"


def _service(sessions) -> TranscriptionDeliveryService:
    """Собрать настоящий транзакционный порт без клиентов моделей."""
    return TranscriptionDeliveryService(
        PostgresTranscriptionReceptionRepository(sessions), worker_token=TOKEN
    )


def _delivery(job, object_key: str) -> TranscriptionDeliveryRequest:
    """Вернуть распознавание с nonce захвата и двумя измеренными ролями."""
    assert job.claim_token is not None
    return TranscriptionDeliveryRequest(
        claim=TranscriptionClaim(
            call_id=job.call_id,
            job_id=job.id,
            claim_token=job.claim_token,
            worker_id=WORKER,
            attempt=job.attempts,
        ),
        source=TranscriptionSource(object_key=object_key, channels=2),
        result=ReceivedTranscription(
            engine="whisper",
            model_name="large-v3",
            model_digest="test-model-digest",
            language="ru",
            role_method="stereo_energy",
            duration_s=4.0,
            replies=(
                ReceivedReply(seq=0, speaker="operator", start_ms=0, end_ms=1500, text=NEW_TEXT),
                ReceivedReply(
                    seq=1, speaker="client", start_ms=1600, end_ms=3000, text="Ответ клиента"
                ),
            ),
        ),
    )


async def _running(sessions):
    """Создать прежний текст/оценку и захват новой расшифровки в backfill с приоритетом 20."""
    call_id = await _call(sessions, "receiving")
    key = f"calls/{call_id}.wav"
    queue = PostgresJobQueue(sessions)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_calls SET audio_object_key = :key, audio_channels = 2, "
                "processing_status = 'transcribing', summary = 'Прежнее резюме', "
                "sentiment = 'negative' WHERE id = :id"
            ),
            {"id": call_id, "key": key},
        )
        await session.execute(
            text(
                "INSERT INTO speech_call_evaluations (id, call_id, script_version, model_name, "
                "script_applies, stages, objections, evaluated_at) "
                "VALUES (:id, :call, 'previous-script', 'test', false, '[]', '[]', now())"
            ),
            {"id": uuid4(), "call": call_id},
        )
        await queue.enqueue(
            session,
            NewJob(
                call_id=call_id,
                kind=JobKind.TRANSCRIBE,
                lane=JobLane.BACKFILL,
                priority=20,
                max_attempts=3,
                lease_seconds=900,
            ),
        )
    job = await queue.claim([JobLane.BACKFILL], WORKER, kinds=[JobKind.TRANSCRIBE])
    assert job is not None
    return queue, _delivery(job, key)


async def _state(sessions, call_id):
    """Снять содержимое бизнес-данных и очереди для проверки отсутствия частичной записи."""
    async with sessions() as session:
        call = (
            await session.execute(
                text(
                    "SELECT processing_status, analysis_status, summary, sentiment, completed_at "
                    "FROM speech_calls WHERE id = :id"
                ),
                {"id": call_id},
            )
        ).one()
        transcript = (
            await session.execute(
                text(
                    "SELECT id, engine, model_name, model_digest, segments_count "
                    "FROM speech_transcripts WHERE call_id = :id"
                ),
                {"id": call_id},
            )
        ).one()
        segments = (
            await session.execute(
                text(
                    "SELECT seq, speaker, text_raw FROM speech_transcript_segments "
                    "WHERE call_id = :id ORDER BY seq"
                ),
                {"id": call_id},
            )
        ).all()
        jobs = (
            await session.execute(
                text(
                    "SELECT id, kind, status, lane, priority, attempts, locked_by, payload "
                    "FROM speech_jobs WHERE call_id = :id ORDER BY kind"
                ),
                {"id": call_id},
            )
        ).all()
        score_count = await session.scalar(
            text("SELECT count(*) FROM speech_call_evaluations WHERE call_id = :id"),
            {"id": call_id},
        )
    return (
        tuple(call),
        tuple(transcript),
        [tuple(row) for row in segments],
        [tuple(row) for row in jobs],
        score_count,
    )


async def test_http_receive_commits_transcript_receipt_and_next_stage(sessions) -> None:
    """Один HTTP-вызов заменяет вход, снимает старую оценку и ставит анализ, не вызывая LLM."""
    _, command = await _running(sessions)
    app = create_app(Settings(_env_file=None, app_env="local", speech_auth_mode="open"))
    app.dependency_overrides[transcription_delivery_dep] = lambda: _service(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    ) as client:
        response = await client.post(
            "/internal/v1/transcription-results",
            json=command.model_dump(mode="json"),
            headers={"Authorization": AUTH},
        )
    assert response.status_code == 200
    receipt = TranscriptionReceipt.model_validate_json(response.content)
    call, transcript, segments, jobs, score_count = await _state(sessions, command.claim.call_id)
    assert call == ("analyzing", "pending", None, None, None)
    assert transcript == (receipt.transcript_id, "whisper", "large-v3", "test-model-digest", 2)
    assert segments == [(0, "operator", NEW_TEXT), (1, "client", "Ответ клиента")]
    assert score_count == 0
    analysis, transcription = jobs
    assert analysis[0] == receipt.analysis_job_id
    assert analysis[1:6] == ("analyze", "queued", "backfill", 20, 0)
    assert transcription[1:3] == ("transcribe", "done") and transcription[6] is None
    stored = transcription[7][TRANSCRIPTION_RECEIPT_KEY]
    assert stored["receipt"]["transcript_id"] == str(receipt.transcript_id)
    assert NEW_TEXT not in str(stored)


async def test_retry_returns_original_receipt_and_changed_body_is_rejected(sessions) -> None:
    """Потеря подтверждения не меняет текст/время и не создаёт второе задание."""
    _, command = await _running(sessions)
    service = _service(sessions)
    first = await service.receive_authorized(command, AUTH)
    before = await _state(sessions, command.claim.call_id)
    assert await service.receive_authorized(command, AUTH) == first
    assert await _state(sessions, command.claim.call_id) == before
    changed = command.model_copy(
        update={
            "result": command.result.model_copy(
                update={
                    "replies": (
                        command.result.replies[0].model_copy(update={"text": "Другой ответ"}),
                    )
                }
            )
        }
    )
    with pytest.raises(ApplicationError) as failure:
        await service.receive_authorized(changed, AUTH)
    assert failure.value.kind is ErrorKind.CONFLICT
    assert await _state(sessions, command.claim.call_id) == before


@pytest.mark.parametrize("field", ["claim_token", "worker_id", "attempt", "source"])
async def test_unaccepted_stale_delivery_preserves_previous_data(sessions, field) -> None:
    """Несовпадающее аудио или захват не затрагивают прежний текст, оценку и очередь."""
    _, command = await _running(sessions)
    before = await _state(sessions, command.claim.call_id)
    if field == "source":
        changed = command.model_copy(
            update={"source": command.source.model_copy(update={"object_key": "another.wav"})}
        )
    else:
        value = {"claim_token": uuid4(), "worker_id": "another-worker", "attempt": 2}[field]
        changed = command.model_copy(
            update={"claim": command.claim.model_copy(update={field: value})}
        )
    with pytest.raises(ApplicationError) as failure:
        await _service(sessions).receive_authorized(changed, AUTH)
    assert failure.value.kind is ErrorKind.CONFLICT
    assert await _state(sessions, command.claim.call_id) == before


async def test_expired_lease_is_rejected_even_before_reaper(sessions) -> None:
    """Статус running сам по себе не разрешает приём после истечения аренды."""
    _, command = await _running(sessions)
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": command.claim.job_id},
        )
    before = await _state(sessions, command.claim.call_id)
    with pytest.raises(ApplicationError) as failure:
        await _service(sessions).receive_authorized(command, AUTH)
    assert failure.value.kind is ErrorKind.CONFLICT
    assert await _state(sessions, command.claim.call_id) == before


async def test_defer_reclaim_same_worker_and_attempt_has_new_nonce(sessions) -> None:
    """Счётчик попытки возвращается при defer; nonce всё равно защищает от старого ответа."""
    queue, command = await _running(sessions)
    assert await queue.defer(command.claim.job_id, WORKER, delay_seconds=0)
    before_claim = (await _state(sessions, command.claim.call_id))[3][0]
    assert CLAIM_TOKEN_KEY not in before_claim[7]
    claimed = await queue.claim([JobLane.BACKFILL], WORKER, kinds=[JobKind.TRANSCRIBE])
    assert claimed is not None and claimed.attempts == command.claim.attempt
    assert claimed.claim_token != command.claim.claim_token
    with pytest.raises(ApplicationError) as failure:
        await _service(sessions).receive_authorized(command, AUTH)
    assert failure.value.kind is ErrorKind.CONFLICT
    accepted = await _service(sessions).receive_authorized(
        _delivery(claimed, command.source.object_key), AUTH
    )
    assert accepted.claim_token == claimed.claim_token


async def test_reprocess_removes_old_receipt_and_rejects_old_attempt(sessions) -> None:
    """Повтор всего этапа не принимает старую квитанцию при тех же id, worker и attempts."""
    queue, command = await _running(sessions)
    service = _service(sessions)
    first = await service.receive_authorized(command, AUTH)
    async with sessions() as session, session.begin():
        await session.execute(text("UPDATE speech_jobs SET status = 'done' WHERE kind = 'analyze'"))
        await reprocess(session, command.claim.call_id, JobKind.TRANSCRIBE, lane=JobLane.BACKFILL)
    claimed = await queue.claim([JobLane.BACKFILL], WORKER, kinds=[JobKind.TRANSCRIBE])
    assert claimed is not None
    assert claimed.id == command.claim.job_id and claimed.attempts == command.claim.attempt
    with pytest.raises(ApplicationError) as failure:
        await service.receive_authorized(command, AUTH)
    assert failure.value.kind is ErrorKind.CONFLICT
    second = await service.receive_authorized(_delivery(claimed, command.source.object_key), AUTH)
    assert second.claim_token != first.claim_token and second.transcript_id != first.transcript_id


async def test_completed_analysis_is_reset_and_queued_old_evaluation_is_removed(sessions) -> None:
    """Уникальность вида задания не должна мешать анализу новой расшифровки."""
    queue, command = await _running(sessions)
    async with sessions() as session, session.begin():
        analysis_id = await queue.enqueue(
            session,
            NewJob(
                call_id=command.claim.call_id,
                kind=JobKind.ANALYZE,
                lane=JobLane.DAILY,
                max_attempts=3,
            ),
        )
        await session.execute(
            text("UPDATE speech_jobs SET status = 'done', attempts = 3 WHERE id = :id"),
            {"id": analysis_id},
        )
        await queue.enqueue(
            session,
            NewJob(
                call_id=command.claim.call_id,
                kind=JobKind.EVALUATE,
                lane=JobLane.DAILY,
                max_attempts=3,
            ),
        )
    receipt = await _service(sessions).receive_authorized(command, AUTH)
    assert receipt.analysis_job_id == analysis_id
    jobs = (await _state(sessions, command.claim.call_id))[3]
    assert [job[1] for job in jobs] == ["analyze", "transcribe"]
    assert jobs[0][2:6] == ("queued", "backfill", 20, 0)


async def test_active_other_stage_does_not_lose_its_input(sessions) -> None:
    """Работающая оценка блокирует замену её реплик; ничего не удаляется."""
    queue, command = await _running(sessions)
    async with sessions() as session, session.begin():
        await queue.enqueue(
            session,
            NewJob(
                call_id=command.claim.call_id,
                kind=JobKind.EVALUATE,
                lane=JobLane.DAILY,
                max_attempts=3,
            ),
        )
    assert await queue.claim([JobLane.DAILY], "quality-test", kinds=[JobKind.EVALUATE]) is not None
    before = await _state(sessions, command.claim.call_id)
    with pytest.raises(ApplicationError) as failure:
        await _service(sessions).receive_authorized(command, AUTH)
    assert failure.value.kind is ErrorKind.CONFLICT
    assert await _state(sessions, command.claim.call_id) == before


async def test_last_lease_check_rolls_back_transcript_and_next_job(sessions, monkeypatch) -> None:
    """Истёкшая к завершению аренда откатывает и замену текста, и снятие старого результата."""
    _, command = await _running(sessions)
    before = await _state(sessions, command.claim.call_id)
    original = PostgresTranscriptionReceptionTransaction.complete

    async def expire_then_complete(self, claim, receipt):
        await self._session.execute(
            text(
                "UPDATE speech_jobs SET locked_at = clock_timestamp() - interval '1 hour' "
                "WHERE id = :id"
            ),
            {"id": claim.job_id},
        )
        return await original(self, claim, receipt)

    monkeypatch.setattr(PostgresTranscriptionReceptionTransaction, "complete", expire_then_complete)
    with pytest.raises(ApplicationError) as failure:
        await _service(sessions).receive_authorized(command, AUTH)
    assert failure.value.kind is ErrorKind.CONFLICT
    assert await _state(sessions, command.claim.call_id) == before


async def test_concurrent_identical_deliveries_return_one_receipt(sessions) -> None:
    """Блокировка звонка сериализует дубли: два отправителя получают один подтверждённый итог."""
    _, command = await _running(sessions)
    service = _service(sessions)
    receipts = await asyncio.gather(
        service.receive_authorized(command, AUTH), service.receive_authorized(command, AUTH)
    )
    assert receipts[0] == receipts[1]
    state = await _state(sessions, command.claim.call_id)
    assert state[1][0] == receipts[0].transcript_id and len(state[3]) == 2


async def test_job_of_another_call_does_not_accept_result(sessions) -> None:
    """Идентификаторы звонка и задания проверяются вместе, до любого изменения текста."""
    _, command = await _running(sessions)
    other_call = await _call(sessions, "other-call")
    before = await _state(sessions, command.claim.call_id)
    changed = command.model_copy(
        update={"claim": command.claim.model_copy(update={"call_id": other_call})}
    )
    with pytest.raises(ApplicationError) as failure:
        await _service(sessions).receive_authorized(changed, AUTH)
    assert failure.value.kind is ErrorKind.NOT_FOUND
    assert await _state(sessions, command.claim.call_id) == before
