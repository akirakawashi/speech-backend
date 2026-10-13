"""Расписание приёма и локальные шаги оценки принадлежат бэкенду."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from src.application.dto.maintenance import MaintenanceRequest
from src.application.dto.quality_batch import LineSnapshot
from src.application.dto.quality_execution import QualityStepRequest
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.quality.preparation import QualityPreparationService
from src.application.quality.scheduling import WorkerMaintenanceService
from src.di.http import create_app
from src.domain.calls.schedule import CollectionSchedule
from src.setting.config import Settings
from tests.test_speech_quality import LINES, _answer

NOW = datetime(2026, 10, 12, 9, tzinfo=UTC)
TOKEN = "test-service-token"
AUTHORIZATION = f"Bearer {TOKEN}"


@pytest.mark.parametrize("action", ["ncc", "telphin"])
def test_maintenance_plan_returns_server_wait_and_ready_parameters(action) -> None:
    """Бэкенд задаёт ожидание и готовые параметры независимо от часов воркера."""
    service = WorkerMaintenanceService(
        CollectionSchedule(), AsyncMock(), AsyncMock(), worker_token=TOKEN
    )
    now = NOW + timedelta(minutes=3)
    plan = service.plan_authorized(
        MaintenanceRequest(action=action, last_started=NOW), AUTHORIZATION, now=now
    )
    if action == "telphin":
        assert plan.wait_seconds == 7 * 3600 - 3 * 60
        assert plan.run_at == NOW + timedelta(hours=7)
        assert plan.ingest is not None
        assert plan.ingest.source.value == "telphin" and plan.ingest.priority == 20
        assert plan.ingest.day_share is None and plan.evaluation is None
    else:
        assert plan.wait_seconds == 12 * 60
        assert plan.run_at == NOW + timedelta(minutes=15)
        assert plan.ingest is not None and plan.ingest.day_share is not None
        assert plan.ingest.source.value == "ncc"
        assert plan.ingest.day_share.share == 0.5 and plan.evaluation is None


@pytest.fixture
def quality():
    """Подставить только хранение; промпты, разбор и повторы остаются настоящими."""
    repository = AsyncMock()
    repository.load_lines.return_value = LineSnapshot(lines=LINES, read_at=NOW)
    service = QualityPreparationService(repository, model="backend-model", max_tokens=2048)
    return service, repository


async def test_quality_returns_initial_prompt_then_review_and_result(quality) -> None:
    """Ответы модели воспроизводятся, проверка выполняется в бэкенде, вызовов LLM нет."""
    service, repository = quality
    command = QualityStepRequest(call_id=uuid4())
    first = await service.prepare(command)
    assert first.request.model == "backend-model"
    assert first.request.max_tokens == 2048
    assert LINES[0].text in first.request.messages[-1].content
    answer = json.dumps(_answer())
    command = command.model_copy(update={"snapshot": first.snapshot, "responses": (answer,)})
    second = await service.prepare(command)
    assert second.result is None
    assert any(message.role == "assistant" for message in second.request.messages)
    command = command.model_copy(update={"responses": (answer, answer)})
    third = await service.prepare(command)
    assert third.request is None
    assert third.result.totals.score == 100
    assert third.result.snapshot == first.snapshot
    repository.save_evaluation.assert_not_awaited()


async def test_invalid_outputs_end_with_normalized_failure(quality) -> None:
    """Исчерпанные повторы формата дают отказ, пригодный для атомарной записи."""
    service, _ = quality
    step = await service.prepare(QualityStepRequest(call_id=uuid4()))
    step = await service.prepare(
        QualityStepRequest(call_id=uuid4(), snapshot=step.snapshot, responses=("bad",) * 3)
    )
    assert step.result.failure_reason == "invalid_evaluation"
    assert step.result.evaluation is None


async def test_known_mono_roles_survive_a_later_model_failure(quality) -> None:
    """Неудавшаяся оценка не теряет роли, которые модель уже определила."""
    service, repository = quality
    repository.load_lines.return_value = LineSnapshot(
        lines=tuple(line.model_copy(update={"speaker": "unknown"}) for line in LINES), read_at=NOW
    )
    first = await service.prepare(QualityStepRequest(call_id=uuid4()))
    roles = json.dumps({"roles": [{"seq": line.seq, "speaker": line.speaker} for line in LINES]})
    final = await service.prepare(
        QualityStepRequest(
            call_id=uuid4(), snapshot=first.snapshot, responses=(roles,), model_error=True
        )
    )
    assert final.result.failure_reason == "model_error"
    assert final.result.roles == tuple((line.seq, line.speaker) for line in LINES)


async def test_changed_transcript_is_rejected_before_preparing_more_prompts(quality) -> None:
    """Результат старого разговора не смешивается с изменёнными репликами."""
    service, repository = quality
    command = QualityStepRequest(call_id=uuid4())
    first = await service.prepare(command)
    repository.load_lines.return_value = LineSnapshot(lines=(), read_at=NOW)
    with pytest.raises(ApplicationError) as failure:
        await service.prepare(command.model_copy(update={"snapshot": first.snapshot}))
    assert failure.value.kind is ErrorKind.CONFLICT


async def test_quality_source_is_checked_in_backend(quality) -> None:
    """Отказ поиска звонка другого источника не позволяет запустить его оценку."""
    service, repository = quality
    repository.find_call.side_effect = LookupError("ncc must not be evaluated")
    with pytest.raises(ApplicationError) as failure:
        await service.prepare(QualityStepRequest(call_id=uuid4()))
    assert failure.value.kind is ErrorKind.NOT_FOUND
    repository.load_lines.assert_not_awaited()


@pytest.mark.parametrize(
    "hour,minute,last,expected",
    [
        (12, 3, None, (12, 3)),
        (12, 3, (12, 0), (12, 15)),
        (12, 36, (12, 0), (12, 36)),
        (23, 59, (23, 45), (0, 0)),
    ],
)
def test_ncc_schedule_aligns_intervals_and_catches_up_once(hour, minute, last, expected) -> None:
    """Первый запуск сразу, следующие на границах четверти часа, простой догоняется один раз."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 10, 12, hour, minute, tzinfo=zone)
    previous = now.replace(hour=last[0], minute=last[1]) if last else None
    result = CollectionSchedule().next_run("ncc", now, previous).astimezone(zone)
    assert (result.hour, result.minute) == expected
    assert result >= now


@pytest.mark.parametrize("hour,expected", [(6, 6), (7, 19), (12, 19), (19, 19), (23, 23)])
def test_telphin_intake_runs_only_at_night(hour, expected) -> None:
    """Пропущенный ночной запуск при рестарте днём ожидает 19:00."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 10, 12, hour, tzinfo=zone)
    result = CollectionSchedule().next_run("telphin", now, None).astimezone(zone)
    assert result.hour == expected


@pytest.mark.parametrize(
    "route,body",
    [
        ("worker-plan", {"action": "ncc"}),
    ],
)
@pytest.mark.parametrize(
    "configured,authorization,status",
    [
        ("", None, 503),
        (TOKEN, None, 401),
        (TOKEN, "Bearer wrong", 401),
    ],
)
async def test_internal_routes_reject_access_before_database_creation(
    route, body, configured, authorization, status
) -> None:
    """Неверный ключ проверяется даже без адреса базы; служебные роуты скрыты из OpenAPI."""
    app = create_app(
        Settings(
            _env_file=None,
            app_env="local",
            speech_auth_mode="open",
            speech_worker_api_token=configured,
        )
    )
    assert not any(path.startswith("/internal/") for path in app.openapi()["paths"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    ) as client:
        response = await client.post(
            f"/internal/v1/{route}",
            json=body,
            headers={"Authorization": authorization} if authorization else {},
        )
    assert response.status_code == status
    assert "wrong" not in response.text


def test_continuation_requires_original_snapshot_and_contract_version() -> None:
    """Несогласованный служебный DTO отсекается до выполнения сценария."""
    with pytest.raises(ValidationError):
        QualityStepRequest(call_id=uuid4(), responses=("answer",))
    with pytest.raises(ValidationError):
        MaintenanceRequest(action="ncc", contract_version="2.0")


def test_invalid_night_window_is_rejected_at_api_startup() -> None:
    """Не ждать первого планового запроса, чтобы обнаружить пустое ночное окно."""
    with pytest.raises(ValueError, match="night window cannot be empty"):
        create_app(
            Settings(
                _env_file=None,
                app_env="local",
                speech_auth_mode="open",
                speech_night_from=7,
                speech_night_until=7,
            )
        )
