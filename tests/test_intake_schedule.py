"""Расписание метаданных в бэкенде: ночь, догон, ошибки и независимость от LLM."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from src.application.dto.calls import CallSource, JobKind, JobLane
from src.application.ingestion.schedule import IntakeSchedule
from src.domain.calls.schedule import CollectionSchedule
from src.infrastructure.runtime.job_processor import JobProcessor


@pytest.mark.parametrize(
    "source,instant,allowed",
    [
        (CallSource.NCC, datetime(2026, 10, 14, 9, tzinfo=UTC), True),
        (CallSource.TELPHIN, datetime(2026, 10, 14, 15, 59, tzinfo=UTC), False),
        (CallSource.TELPHIN, datetime(2026, 10, 14, 16, tzinfo=UTC), True),
        (CallSource.TELPHIN, datetime(2026, 10, 15, 3, 59, tzinfo=UTC), True),
        (CallSource.TELPHIN, datetime(2026, 10, 15, 4, tzinfo=UTC), False),
    ],
)
async def test_intake_schedule_keeps_source_night_boundary_and_retries_failure(
    monkeypatch, source, instant, allowed
):
    """За границами ночи Телфин не опрашивается; сбой не сдвигает срок повтора."""
    catch = AsyncMock()
    monkeypatch.setattr("src.application.ingestion.schedule.catch_up", catch)
    source_factory = Mock()
    schedule = IntakeSchedule(source, CollectionSchedule(), Mock(), Mock(), source_factory)
    await schedule.tick(now=instant)
    assert catch.await_count == int(allowed) and source_factory.call_count == int(allowed)
    if allowed:
        call = catch.call_args.kwargs
        assert call["today"] == instant.astimezone(ZoneInfo("Europe/Moscow")).date()
        assert call["priority"] == (20 if source is CallSource.TELPHIN else None)
        assert (call["day_share"] is not None) == (source is CallSource.NCC)
        await schedule.tick(now=instant + timedelta(seconds=1))
        assert catch.await_count == 1
        later = instant + (
            timedelta(days=1) if source is CallSource.TELPHIN else timedelta(hours=1)
        )
        catch.side_effect = RuntimeError("private-provider-error")
        with pytest.raises(RuntimeError):
            await schedule.tick(now=later)
        catch.side_effect = None
        await schedule.tick(now=later)
        assert catch.await_count == 3


async def test_slow_intake_does_not_block_other_intake_quality_or_processing():
    """У каждого расписания свой цикл; остановка отменяет опрос источника, сохраняя LLM."""
    blocked = asyncio.Event()
    started = asyncio.Event()
    other = asyncio.Event()
    scheduled = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_tick():
        started.set()
        try:
            await blocked.wait()
        finally:
            cancelled.set()

    async def other_tick():
        await started.wait()
        other.set()

    async def quality_tick():
        await started.wait()
        scheduled.set()

    queue = SimpleNamespace(
        requeue_expired=AsyncMock(return_value=[]), claim=AsyncMock(return_value=None)
    )
    processor = JobProcessor(
        queue,
        {JobKind.ANALYZE: Mock()},
        lanes=[JobLane.DAILY],
        schedule=SimpleNamespace(tick=quality_tick),
        schedules=[SimpleNamespace(tick=slow_tick), SimpleNamespace(tick=other_tick)],
        idle_seconds=0.01,
        reaper_seconds=10,
    )
    task = asyncio.create_task(processor.run_forever())
    try:
        await asyncio.wait_for(asyncio.gather(other.wait(), scheduled.wait()), timeout=2)
        await asyncio.sleep(0.02)
        assert queue.claim.await_count > 0
    finally:
        processor.request_stop()
        await asyncio.wait_for(task, timeout=2)
    assert cancelled.is_set()
