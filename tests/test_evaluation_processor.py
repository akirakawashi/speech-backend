"""CPU-оценка: атомарный исход, допуск, корректный ввод, расписание и ресурсы DI."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest

from src.application.dto.calls import JobErrorClass, JobKind, JobLane, JobStatus
from src.application.dto.evaluation_processing import EvaluationInput, EvaluationSnapshot
from src.application.dto.job_processing import (
    JobProcessingClaim,
    JobProcessingLease,
    JobProcessingOutcome,
    SpeechProcessorOptions,
)
from src.application.dto.quality import CURRENT_SCRIPT, TranscriptLine
from src.application.dto.quality_execution import QualityWork
from src.application.quality.processing import EvaluationProcessingService
from src.application.quality.processor_schedule import EvaluationProcessorSchedule
from src.di import speech_processor as di
from src.domain.calls.schedule import CollectionSchedule
from src.infrastructure.runtime.job_processor import JobProcessor
from src.presentation.cli.speech_processor import main
from src.setting.config import Settings
from tests.test_speech_quality_db import _answer, _FakeModel


@pytest.fixture
def context():
    """Подставить только UoW; вычисление и правила оценки остаются настоящими."""
    claim = JobProcessingClaim(
        call_id=uuid4(),
        job_id=uuid4(),
        claim_token=uuid4(),
        worker_id="cpu-evaluation",
        attempt=1,
        kind=JobKind.EVALUATE,
    )
    now = datetime.now(UTC)
    data = EvaluationInput(
        call_source="telphin",
        processing_status="done",
        transcript_id=uuid4(),
        transcript_created_at=now - timedelta(seconds=5),
        role_method="stereo_energy",
        lines=(
            TranscriptLine(seq=0, speaker="operator", start_ms=0, text="PRIVATE_OPERATOR_TEXT"),
            TranscriptLine(seq=1, speaker="client", start_ms=1000, text="PRIVATE_CLIENT_TEXT"),
        ),
    )
    snapshot = EvaluationSnapshot(data=data, read_at=now, max_attempts=3)
    lease = JobProcessingLease(
        call_id=claim.call_id,
        job_id=claim.job_id,
        kind="evaluate",
        status="running",
        claim_token=claim.claim_token,
        locked_by=claim.worker_id,
        attempt=1,
        lease_until=now + timedelta(minutes=10),
    )
    transaction = SimpleNamespace(
        lock=AsyncMock(return_value=lease),
        database_time=AsyncMock(return_value=now),
        snapshot=AsyncMock(return_value=data),
        newer_success=AsyncMock(return_value=False),
        save=AsyncMock(),
        complete=AsyncMock(return_value=True),
        fail=AsyncMock(return_value=JobStatus.DEAD),
    )
    committed = []

    @asynccontextmanager
    async def unit_of_work():
        """Отметить commit только после выхода application из блока записи."""
        yield transaction
        committed.append(True)

    repository = SimpleNamespace(
        load=AsyncMock(return_value=snapshot),
        transaction=unit_of_work,
        defer=AsyncMock(return_value=True),
    )
    model = _FakeModel(_answer())
    return SimpleNamespace(
        claim=claim,
        snapshot=snapshot,
        transaction=transaction,
        repository=repository,
        model=model,
        committed=committed,
    )


def _service(context):
    """Собрать сценарий с реальными правилами и контролируемым хранением."""
    return EvaluationProcessingService(context.repository, context.model)


async def test_evaluation_done_follows_commit(context) -> None:
    """Успех означает commit; модель получает оценку и проверку, хранение получает DTO."""
    result = await _service(context).process(context.claim)
    assert result.status == "done" and context.committed == [True]
    assert len(context.model.prompts) == 2
    work = context.transaction.save.await_args.args[1]
    assert work.evaluation is not None and work.script_version == CURRENT_SCRIPT.version
    context.transaction.complete.assert_awaited_once_with(context.claim)


async def test_commit_failure_is_not_reported_as_done(context) -> None:
    """Ошибка commit выходит вызывающему вместо ложного подтверждения результата."""

    @asynccontextmanager
    async def failing_commit():
        """Показать вычисленный результат, затем отклонить commit."""
        yield context.transaction
        raise OSError("database unavailable")

    context.repository.transaction = failing_commit
    with pytest.raises(OSError):
        await _service(context).process(context.claim)
    assert not context.committed


@pytest.mark.parametrize("change", ["text", "role_method", "identity", "source"])
async def test_changed_input_defers_without_storing(context, change) -> None:
    """Версия входа проверяется перед записью; прежний ответ модели не попадает в базу."""
    data = context.snapshot.data
    updates = (
        {"role_method": "llm_attribution"}
        if change == "role_method"
        else (
            {"transcript_id": uuid4()}
            if change == "identity"
            else (
                {"call_source": "ncc"}
                if change == "source"
                else {
                    "lines": (
                        data.lines[0].model_copy(update={"text": "CHANGED"}),
                        data.lines[1],
                    )
                }
            )
        )
    )
    context.transaction.snapshot.return_value = data.model_copy(update=updates)
    assert (await _service(context).process(context.claim)).status == "deferred"
    context.transaction.save.assert_not_awaited()
    context.transaction.complete.assert_not_awaited()
    context.transaction.fail.assert_not_awaited()
    context.repository.defer.assert_awaited_once_with(context.claim)


@pytest.mark.parametrize("change", ["nonce", "owner", "attempt", "expiry", "kind"])
async def test_lost_lease_never_changes_results(context, change) -> None:
    """Даже пригодный ответ не даёт права записи устаревшему захвату."""
    updates = {
        "nonce": {"claim_token": uuid4()},
        "owner": {"locked_by": "other"},
        "attempt": {"attempt": 2},
        "expiry": {"lease_until": context.snapshot.read_at},
        "kind": {"kind": JobKind.ANALYZE},
    }[change]
    context.transaction.lock.return_value = context.transaction.lock.return_value.model_copy(
        update=updates
    )
    assert (await _service(context).process(context.claim)).status == "stale"
    context.transaction.save.assert_not_awaited()
    context.transaction.complete.assert_not_awaited()
    context.transaction.fail.assert_not_awaited()


async def test_newer_success_finishes_job_without_overwriting_grade(context) -> None:
    """Параллельно поставленная более новая оценка сохраняется; задание подтверждается."""
    context.transaction.newer_success.return_value = True
    assert (await _service(context).process(context.claim)).status == "done"
    context.transaction.save.assert_not_awaited()
    context.transaction.complete.assert_awaited_once()


async def test_ncc_is_rejected_before_any_model_request(context) -> None:
    """NCC не отправляется на оценку продаж даже при вручную созданном задании."""
    data = context.snapshot.data.model_copy(update={"call_source": "ncc"})
    context.repository.load.return_value = context.snapshot.model_copy(update={"data": data})
    context.transaction.snapshot.return_value = data
    result = await _service(context).process(context.claim)
    assert result.status == "dead" and result.error_class is JobErrorClass.NOT_FOUND
    assert not context.model.prompts
    context.transaction.save.assert_not_awaited()


@pytest.mark.parametrize("reason", ["pending", "upstream", "future_transcript"])
async def test_not_ready_input_waits_without_requesting_model(context, reason) -> None:
    """Не завершённый предшествующий этап и будущая версия не расходуют модель/попытку."""
    snapshot = context.snapshot
    if reason == "upstream":
        snapshot = snapshot.model_copy(update={"other_processing": True})
    else:
        changes = (
            {"processing_status": "queued"}
            if reason == "pending"
            else {"transcript_created_at": snapshot.read_at + timedelta(seconds=1)}
        )
        snapshot = snapshot.model_copy(update={"data": snapshot.data.model_copy(update=changes)})
    context.repository.load.return_value = snapshot
    assert (await _service(context).process(context.claim)).status == "deferred"
    assert not context.model.prompts
    context.transaction.save.assert_not_awaited()


async def test_invalid_work_snapshot_is_never_saved(context, monkeypatch) -> None:
    """Сломанный адаптер не может передать итог по чужому снимку даже как валидный Pydantic DTO."""
    work = QualityWork(
        snapshot=context.snapshot.lines.model_copy(update={"lines": ()}),
        model_name=context.model.model,
        script_version=CURRENT_SCRIPT.version,
        failure_reason="model_error",
    )
    monkeypatch.setattr(
        "src.application.quality.processing.compute_quality", AsyncMock(return_value=work)
    )
    assert (await _service(context).process(context.claim)).status == "dead"
    context.transaction.save.assert_not_awaited()


async def test_schedule_is_local_bounded_and_keeps_due_after_error() -> None:
    """Один догон/порция на интервал; сбой постановки не теряет срок и не требует токен."""
    maintenance = SimpleNamespace(
        enqueue=AsyncMock(side_effect=[OSError(), SimpleNamespace(), SimpleNamespace()])
    )
    scheduler = EvaluationProcessorSchedule(maintenance, CollectionSchedule(quality_limit=37))
    now = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    with pytest.raises(OSError):
        await scheduler.tick(now=now)
    await scheduler.tick(now=now)
    await scheduler.tick(now=now + timedelta(seconds=1))
    assert maintenance.enqueue.await_count == 2
    selection = maintenance.enqueue.await_args.args[0].selection
    assert (
        selection.limit == 37 and selection.skip_failed and selection.everything and selection.apply
    )
    await scheduler.tick(now=now + timedelta(minutes=15))
    assert maintenance.enqueue.await_count == 3


async def test_schedule_runs_while_model_waits_and_stop_prevents_next_claim() -> None:
    """Проверить постановку во время LLM и остановку без нового захвата."""
    entered = asyncio.Event()
    release = asyncio.Event()
    scheduled_during_model = asyncio.Event()
    job = SimpleNamespace(
        id=uuid4(),
        call_id=uuid4(),
        claim_token=uuid4(),
        kind=JobKind.EVALUATE,
        attempts=1,
        lease_seconds=900,
    )

    async def process(claim):
        """Удержать модель до сигнала теста, не блокируя event loop."""
        entered.set()
        await release.wait()
        return JobProcessingOutcome(status="done")

    async def tick():
        """Зафиксировать постановку, которая произошла во время обработки задания."""
        if entered.is_set():
            scheduled_during_model.set()

    queue = SimpleNamespace(
        claim=AsyncMock(return_value=job),
        heartbeat=AsyncMock(return_value=True),
        requeue_expired=AsyncMock(return_value=[]),
    )
    processor = JobProcessor(
        queue,
        {JobKind.EVALUATE: SimpleNamespace(process=process)},
        lanes=(JobLane.DAILY,),
        schedule=SimpleNamespace(tick=tick),
        idle_seconds=0.01,
        heartbeat_seconds=0.01,
        reaper_seconds=0.05,
    )
    task = asyncio.create_task(processor.run_forever())
    try:
        await asyncio.wait_for(scheduled_during_model.wait(), timeout=2)
        processor.request_stop()
        release.set()
        await asyncio.wait_for(task, timeout=2)
        assert queue.claim.await_count == 1
    finally:
        release.set()
        task.cancel()
        if not task.done():
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("argv", [["--stages", "transcribe"], ["--stages", ""], ["--lanes", "bad"]])
def test_invalid_cli_does_not_open_dependencies(monkeypatch, argv) -> None:
    """Неверный этап/полоса отклоняются до DI; CPU не умеет получать или распознавать аудио."""
    run = Mock()
    monkeypatch.setattr(di, "run", run)
    with pytest.raises(SystemExit) as error:
        main(argv)
    assert error.value.code == 2
    run.assert_not_called()


@pytest.mark.parametrize("ready", [True, False])
async def test_evaluate_only_di_has_no_analysis_or_worker_dependencies(monkeypatch, ready) -> None:
    """Оценка требует только свою модель/базу; пул закрывается и при несовместимой схеме."""
    engine = SimpleNamespace(dispose=AsyncMock())
    sessions = MagicMock()
    sessions.return_value.__aenter__ = AsyncMock(return_value=SimpleNamespace())
    sessions.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(di, "get_speech_ms_engine", lambda _: engine)
    monkeypatch.setattr(di, "get_speech_ms_sessionmaker", lambda _: sessions)
    monkeypatch.setattr(di, "speech_schema_is_ready", AsyncMock(return_value=ready))
    analysis = Mock(side_effect=AssertionError("analysis must not be constructed"))
    monkeypatch.setattr(di, "build_analysis_client", analysis)
    model = SimpleNamespace(model="test-model")
    monkeypatch.setattr(di, "build_quality_client", lambda _: model)
    processor = SimpleNamespace(run_once=AsyncMock(), run_forever=AsyncMock())
    factory = Mock(return_value=processor)
    monkeypatch.setattr(di, "JobProcessor", factory)
    monkeypatch.setattr(di, "install_signal_handlers", lambda _: None)
    options = SpeechProcessorOptions(
        stages=(JobKind.EVALUATE,), once=True, schedule_evaluations=False
    )
    settings = Settings(_env_file=None, speech_worker_api_token="")
    if ready:
        await di.run_speech_processor(options, settings)
        processor.run_once.assert_awaited_once()
        assert set(factory.call_args.args[1]) == {JobKind.EVALUATE}
        assert factory.call_args.kwargs["schedule"] is None
    else:
        with pytest.raises(RuntimeError, match="schema"):
            await di.run_speech_processor(options, settings)
        factory.assert_not_called()
    analysis.assert_not_called()
    engine.dispose.assert_awaited_once()
