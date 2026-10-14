"""CPU-запуск: типизированный вход, жизненный цикл, heartbeat и транзакционные исходы."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.application.analysis.execution import AnalysisExecutionService
from src.application.analysis.processing import AnalysisProcessingService
from src.application.dto.analysis_processing import (
    AnalysisClaim,
    AnalysisLease,
    AnalysisOutcome,
    AnalysisProcessorOptions,
    AnalysisSnapshot,
)
from src.application.dto.calls import JobErrorClass, JobKind, JobLane
from src.application.dto.pipeline import AnalyzeWork
from src.application.dto.pipeline_input import (
    AnalysisInput,
    AnalysisSegment,
    CallForAnalysis,
    ClassifierRecord,
)
from src.application.errors.stages import StageError
from src.application.ports.queue import CLAIM_TOKEN_KEY, ClaimedJob
from src.di import analysis_processor as di
from src.infrastructure.runtime.analysis_processor import AnalysisProcessor
from src.presentation.cli.analysis_processor import main
from src.setting.config import Settings
from tests.test_speech_analyze import _nodes


def _claim() -> AnalysisClaim:
    """Выдать согласованный UUID/nonce текущего владельца."""
    return AnalysisClaim(
        call_id=uuid4(), job_id=uuid4(), claim_token=uuid4(), worker_id="cpu-test", attempt=1
    )


def _snapshot(claim) -> AnalysisSnapshot:
    """Вернуть снимок с синтетическим текстом и настоящей формой классификатора."""
    return AnalysisSnapshot(
        transcript_id=uuid4(),
        transcript_created_at=datetime.now(UTC),
        data=AnalysisInput(
            call=CallForAnalysis(id=claim.call_id, role_method="none"),
            segments=(AnalysisSegment(seq=0, speaker="unknown", text_raw="PRIVATE_TRANSCRIPT"),),
            classifier=tuple(ClassifierRecord.model_validate(row) for row in _nodes()),
            reasons=(),
        ),
    )


@pytest.mark.parametrize("empty", [False, True])
async def test_missing_model_preserves_previous_failure_precedence(empty: bool) -> None:
    """Пустой текст остаётся NOT_FOUND даже без клиента; имеющийся текст требует модель."""
    claim = _claim()
    data = _snapshot(claim).data
    if empty:
        data = data.model_copy(update={"segments": ()})
    with pytest.raises(StageError) as raised:
        await AnalysisExecutionService().execute(claim.call_id, data, None)
    assert raised.value.error_class is (
        JobErrorClass.NOT_FOUND if empty else JobErrorClass.PROVIDER_ERROR
    )


def _lease(claim) -> AnalysisLease:
    """Вернуть живую аренду по часам условной базы."""
    return AnalysisLease(
        call_id=claim.call_id,
        job_id=claim.job_id,
        kind="analyze",
        status="running",
        claim_token=claim.claim_token,
        locked_by=claim.worker_id,
        attempt=claim.attempt,
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.fixture
def context():
    """Подставить UoW с контролируемым commit; вычисление не связано с ресурсами аудио."""
    claim = _claim()
    snapshot = _snapshot(claim)
    transaction = SimpleNamespace(
        lock=AsyncMock(return_value=_lease(claim)),
        database_time=AsyncMock(side_effect=lambda: datetime.now(UTC)),
        snapshot=AsyncMock(return_value=snapshot),
        save=AsyncMock(),
        complete=AsyncMock(return_value=True),
        fail=AsyncMock(),
        mark_failed=AsyncMock(),
    )
    state = {"committed": False}

    @asynccontextmanager
    async def unit():
        yield transaction
        state["committed"] = True

    repo = SimpleNamespace(
        load=AsyncMock(return_value=snapshot), transaction=unit, defer=AsyncMock(return_value=True)
    )
    execution = SimpleNamespace(
        execute=AsyncMock(
            return_value=AnalyzeWork(node_id=None, path=(), sentiment=None, summary="summary")
        )
    )
    service = AnalysisProcessingService(repo, Mock(), execution)
    return SimpleNamespace(
        claim=claim,
        snapshot=snapshot,
        tx=transaction,
        state=state,
        repo=repo,
        execution=execution,
        service=service,
    )


async def test_outcome_only_after_commit(context) -> None:
    """Завершение анализа подтверждается после выхода из UoW, не после SQL store."""
    assert (await context.service.process(context.claim)).status == "done"
    assert context.state["committed"]
    context.tx.save.assert_awaited_once()
    context.tx.complete.assert_awaited_once()


async def test_failed_commit_never_reports_done(context) -> None:
    """Ошибка завершения транзакции выходит runtime; успешный исход не выдаётся."""

    @asynccontextmanager
    async def broken():
        yield context.tx
        raise RuntimeError("synthetic commit failure")

    context.repo.transaction = broken
    with pytest.raises(RuntimeError, match="commit failure"):
        await context.service.process(context.claim)
    assert not context.state["committed"]


async def test_changed_snapshot_defers_instead_of_storing(context) -> None:
    """Изменённый текст запрещает запись старого ответа и не расходует попытку."""
    replacement = context.snapshot.model_copy(update={"transcript_id": uuid4()})
    context.tx.snapshot.return_value = replacement
    assert (await context.service.process(context.claim)).status == "deferred"
    context.tx.save.assert_not_awaited()
    context.tx.fail.assert_not_awaited()
    context.repo.defer.assert_awaited_once_with(context.claim)


@pytest.mark.parametrize(
    "change",
    [
        {"claim_token": uuid4()},
        {"attempt": 2},
        {"locked_by": "other"},
        {"lease_until": datetime.now(UTC) - timedelta(minutes=1)},
    ],
)
async def test_stale_claim_never_writes(context, change) -> None:
    """Идентичное имя процесса не заменяет nonce; просроченное право записи отклоняется."""
    context.tx.lock.return_value = _lease(context.claim).model_copy(update=change)
    assert (await context.service.process(context.claim)).status == "stale"
    context.tx.save.assert_not_awaited()
    context.tx.fail.assert_not_awaited()


def test_fingerprint_ignores_query_row_order_but_tracks_content() -> None:
    """Снимок устойчив к порядку SELECT; изменение ролей/текста и версии транскрипта значимо."""
    snapshot = _snapshot(_claim())
    reordered = snapshot.model_copy(
        update={
            "data": snapshot.data.model_copy(
                update={"classifier": tuple(reversed(snapshot.data.classifier))}
            )
        }
    )
    assert reordered.fingerprint == snapshot.fingerprint
    changed = snapshot.model_copy(
        update={
            "data": snapshot.data.model_copy(
                update={
                    "segments": (
                        AnalysisSegment(seq=0, speaker="operator", text_raw="PRIVATE_TRANSCRIPT"),
                    )
                }
            )
        }
    )
    assert changed.fingerprint != snapshot.fingerprint


@pytest.mark.parametrize(
    "args", [["--lanes", ""], ["--lanes", "unknown"], ["--stages", "transcribe"]]
)
def test_invalid_cli_does_not_reach_di(monkeypatch, args) -> None:
    """Неверные параметры не открывают базу и не читают ключи моделей."""
    runner = Mock()
    monkeypatch.setattr(di, "run", runner)
    with pytest.raises(SystemExit) as result:
        main(args)
    assert result.value.code == 2
    runner.assert_not_called()


def test_options_have_no_arbitrary_stages() -> None:
    """Параметры не позволяют случайно включить аудио или оценку."""
    options = AnalysisProcessorOptions(lanes="backfill,daily,backfill")
    assert options.lanes == (JobLane.BACKFILL, JobLane.DAILY)
    with pytest.raises(ValidationError):
        AnalysisProcessorOptions.model_validate({"stages": "evaluate"})


@pytest.fixture
def runtime():
    """Подставить очередь и application, сохранив настоящий цикл heartbeat/остановки."""
    job = ClaimedJob(
        id=uuid4(),
        call_id=uuid4(),
        kind=JobKind.ANALYZE,
        lane=JobLane.DAILY,
        attempts=1,
        max_attempts=3,
        payload={CLAIM_TOKEN_KEY: str(uuid4())},
        lease_seconds=300,
    )
    queue = SimpleNamespace(
        claim=AsyncMock(side_effect=[job, None]),
        heartbeat=AsyncMock(return_value=True),
        requeue_expired=AsyncMock(return_value=[]),
    )
    application = SimpleNamespace(
        process=AsyncMock(return_value=AnalysisOutcome(status="done")),
        mark_expired=AsyncMock(),
        repair_expired_failures=AsyncMock(),
    )
    processor = AnalysisProcessor(
        queue,
        application,
        lanes=[JobLane.DAILY],
        worker_id="cpu-test",
        heartbeat_seconds=0.01,
        idle_seconds=0.01,
    )
    return SimpleNamespace(job=job, queue=queue, application=application, processor=processor)


async def test_stop_finishes_active_job_and_claims_no_more(runtime) -> None:
    """SIGTERM семантически останавливает новые захваты, сохраняя текущий анализ."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def process(claim):
        entered.set()
        await release.wait()
        return AnalysisOutcome(status="done")

    runtime.application.process.side_effect = process
    task = asyncio.create_task(runtime.processor.run_forever())
    await asyncio.wait_for(entered.wait(), timeout=1)
    runtime.processor.request_stop()
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, timeout=1)
    runtime.queue.claim.assert_awaited_once_with(
        (JobLane.DAILY,), "cpu-test", kinds=(JobKind.ANALYZE,)
    )


async def test_lost_heartbeat_cancels_stale_model_work(runtime) -> None:
    """Другой nonce прекращает устаревший HTTP-вызов; данные не помечаются неудавшимися."""
    cancelled = asyncio.Event()

    async def process(claim):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime.application.process.side_effect = process
    runtime.queue.heartbeat.return_value = False
    assert await asyncio.wait_for(runtime.processor.run_once(), timeout=1)
    assert cancelled.is_set()
    runtime.queue.heartbeat.assert_awaited_once_with(
        runtime.job.id, "cpu-test", claim_token=runtime.job.claim_token
    )


async def test_three_failed_heartbeats_are_safe_and_bounded(runtime, caplog) -> None:
    """Обрыв БД отменяет работу после трёх отказов; текст исключения в лог не попадает."""

    async def blocked(claim):
        await asyncio.Event().wait()

    runtime.application.process.side_effect = blocked
    runtime.queue.heartbeat.side_effect = RuntimeError("PRIVATE_TRANSCRIPT and secret")
    assert await asyncio.wait_for(runtime.processor.run_once(), timeout=1)
    assert runtime.queue.heartbeat.await_count == 3
    assert "PRIVATE_TRANSCRIPT" not in caplog.text and "secret" not in caplog.text


async def test_di_once_disposes_pool_without_audio_dependencies(monkeypatch) -> None:
    """CPU-ресурсы создаются в event loop; ключи API/Whisper/MinIO не требуются."""
    session = MagicMock()
    session.__aenter__.return_value = session
    sessions = Mock(return_value=session)
    engine = SimpleNamespace(dispose=AsyncMock())
    processor = SimpleNamespace(run_once=AsyncMock(), run_forever=AsyncMock())
    factory = Mock(return_value=processor)
    monkeypatch.setattr(di, "get_speech_ms_engine", Mock(return_value=engine))
    monkeypatch.setattr(di, "get_speech_ms_sessionmaker", Mock(return_value=sessions))
    monkeypatch.setattr(di, "speech_schema_is_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(di, "AnalysisProcessor", factory)
    monkeypatch.setattr(di, "install_signal_handlers", Mock())
    settings = Settings(
        _env_file=None,
        speech_analysis_llm_base_url="http://model/v1",
        speech_worker_api_token="",
        speech_whisper_base_url="",
        speech_storage_base_url="",
    )
    await di.run_analysis_processor(AnalysisProcessorOptions(once=True), settings)
    processor.run_once.assert_awaited_once()
    engine.dispose.assert_awaited_once()
    assert isinstance(factory.call_args.args[1]._execution, AnalysisExecutionService)


async def test_di_incompatible_schema_stops_before_claim_and_closes_pool(monkeypatch) -> None:
    """Необновлённая база останавливает старт до создания исполнителя."""
    session = MagicMock()
    session.__aenter__.return_value = session
    engine = SimpleNamespace(dispose=AsyncMock())
    factory = Mock()
    monkeypatch.setattr(di, "get_speech_ms_engine", Mock(return_value=engine))
    monkeypatch.setattr(
        di, "get_speech_ms_sessionmaker", Mock(return_value=Mock(return_value=session))
    )
    monkeypatch.setattr(di, "speech_schema_is_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(di, "AnalysisProcessor", factory)
    with pytest.raises(RuntimeError, match="schema is not ready"):
        await di.run_analysis_processor(
            AnalysisProcessorOptions(once=True),
            Settings(_env_file=None, speech_analysis_llm_base_url="http://model/v1"),
        )
    factory.assert_not_called()
    engine.dispose.assert_awaited_once()
