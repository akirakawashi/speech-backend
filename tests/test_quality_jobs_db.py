"""Постановка оценки в общую очередь на настоящем PostgreSQL."""

from datetime import timedelta

import pytest
from sqlalchemy import text

from src.application.dto.quality_batch import CandidateSelection
from src.infrastructure.persistence.quality_jobs import PostgresQualityQueueRepository
from tests.speech_db_support import speech_test_database_url
from tests.test_speech_quality_db import START, _call, sessions as sessions

pytestmark = pytest.mark.skipif(
    not speech_test_database_url(), reason="SPEECH_POSTGRES_TEST_URL is required"
)


async def test_only_done_telphin_calls_are_queued_and_dry_run_does_not_write(sessions) -> None:
    """NCC, незавершённый звонок и звонок без расшифровки не становятся кандидатами."""
    call = await _call(sessions, "eligible")
    await _call(sessions, "ncc", source="ncc")
    await _call(sessions, "no-text", transcribed=False)
    pending = await _call(sessions, "pending")
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_calls SET processing_status = 'analyzing' WHERE id = :id"),
            {"id": pending},
        )
    repository = PostgresQualityQueueRepository(sessions)
    selection = CandidateSelection(skip_failed=True, limit=100)
    report = await repository.schedule(selection, call_id=None, apply=False)
    assert report.candidates == 1 and report.enqueued == 0
    report = await repository.schedule(selection, call_id=None, apply=True)
    assert report.enqueued == 1
    async with sessions() as session:
        row = (await session.execute(text("SELECT call_id, kind, priority FROM speech_jobs"))).one()
        assert tuple(row) == (call, "evaluate", 30)


async def test_already_queued_calls_do_not_consume_next_batch_limit(sessions) -> None:
    """Следующие порции добирают архив, вместо повторного просмотра первых занятых звонков."""
    for index in range(5):
        await _call(sessions, f"batch-{index}", started=START + timedelta(minutes=index))
    repository = PostgresQualityQueueRepository(sessions)
    selection = CandidateSelection(skip_failed=True, limit=2)
    reports = [await repository.schedule(selection, call_id=None, apply=True) for _ in range(4)]
    assert [report.enqueued for report in reports] == [2, 2, 1, 0]


async def test_dead_quality_job_is_skipped_until_transcript_changes(sessions) -> None:
    """Фоновый план не оживляет исчерпанное задание каждые 15 минут."""
    call = await _call(sessions, "dead")
    repository = PostgresQualityQueueRepository(sessions)
    selection = CandidateSelection(skip_failed=True, limit=100)
    assert (await repository.schedule(selection, call_id=None, apply=True)).enqueued == 1
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET status = 'dead', attempts = max_attempts")
        )
    assert (await repository.schedule(selection, call_id=None, apply=True)).enqueued == 0
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_transcripts SET created_at = clock_timestamp() WHERE call_id = :id"
            ),
            {"id": call},
        )
    assert (await repository.schedule(selection, call_id=None, apply=True)).enqueued == 1
    async with sessions() as session:
        assert (await session.execute(text("SELECT status, attempts FROM speech_jobs"))).one() == (
            "queued",
            0,
        )
