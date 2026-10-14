"""Приём метаданных: курсор, ограничения, дневная доля и приоритеты на PostgreSQL.

Технический исполнитель проверяется в speech-worker; здесь аудио не открывается.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.dto.daily_ingest import DayShare
from src.di.ingestion import run_day
from src.domain.calls.contracts import (
    JobKind,
    JobLane,
)
from src.infrastructure.persistence.daily_ingest import CURSOR_SOURCE
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.infrastructure.sources.ncc.source import LocalExportSource
from tests.speech_db_support import speech_test_database_url

_TEST_DATABASE_URL = speech_test_database_url()
pytestmark = pytest.mark.skipif(
    not _TEST_DATABASE_URL,
    reason="SPEECH_POSTGRES_TEST_URL is required",
)

KEY = "ncc-tm%2F2026%2F01%2F14%2Fnode_1_domain_2_nauss_0_1766092829_1138986.mp3"
KEY_B = "ncc-tm%2F2026%2F01%2F14%2Fnode_2_domain_1_nauss_1_1766092823_1603045.mp3"
DAY = date(2026, 1, 14)


@pytest.fixture()
async def env(tmp_path: Path) -> AsyncIterator[tuple[PostgresJobQueue, async_sessionmaker, Path]]:
    assert _TEST_DATABASE_URL is not None
    engine = create_async_engine(_TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _clean() -> None:
        async with sessions() as session, session.begin():
            await session.execute(text("DELETE FROM speech_jobs"))
            await session.execute(text("DELETE FROM speech_calls"))
            await session.execute(text("DELETE FROM speech_ingest_cursors"))

    await _clean()
    (tmp_path / KEY).write_bytes(b"ID3fake")
    (tmp_path / KEY_B).write_bytes(b"ID3fake")
    yield PostgresJobQueue(sessions, engine=engine), sessions, tmp_path
    await _clean()
    await engine.dispose()


async def _seed_day(queue: PostgresJobQueue, sessions: async_sessionmaker, export: Path) -> None:
    await run_day(DAY, source=LocalExportSource(export), sessionmaker=sessions, queue=queue)


# --------------------------------------------------------------------------- #
# Суточный прогон
# --------------------------------------------------------------------------- #


async def test_daily_run_registers_calls_and_queues_them(env) -> None:
    queue, sessions, export = env

    report = await run_day(
        DAY, source=LocalExportSource(export), sessionmaker=sessions, queue=queue
    )

    assert report.discovered == 2
    assert report.enqueued == 2
    assert report.already_known == 0
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM speech_calls")) == 2
        assert (
            await session.scalar(text("SELECT count(*) FROM speech_jobs WHERE kind = 'ingest'"))
            == 2
        )


async def test_running_the_same_day_twice_changes_nothing(env) -> None:
    """Повторный прогон за ту же дату — обычное дело: перезапуск после сбоя."""
    queue, sessions, export = env
    await _seed_day(queue, sessions, export)

    second = await run_day(
        DAY, source=LocalExportSource(export), sessionmaker=sessions, queue=queue
    )

    assert second.discovered == 2
    assert second.enqueued == 0, "второй раз задания не ставятся"
    assert second.already_known == 2
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM speech_calls")) == 2


async def test_the_processed_day_is_recorded(env) -> None:
    """Чтобы «звонков не было» отличалось от «прогон не запускался»."""
    queue, sessions, export = env
    await _seed_day(queue, sessions, export)

    async with sessions() as session:
        stored = await session.scalar(
            text("SELECT last_processed_day FROM speech_ingest_cursors WHERE source = :s"),
            {"s": CURSOR_SOURCE},
        )
    assert stored == DAY


async def test_time_precision_is_recorded_when_the_source_has_no_clock(env) -> None:
    """Выгрузка знает только дату — и это записано, а не замаскировано полночью."""
    queue, sessions, export = env
    await _seed_day(queue, sessions, export)

    async with sessions() as session:
        precision = await session.scalar(
            text("SELECT source_snapshot->>'time_precision' FROM speech_calls LIMIT 1")
        )
    assert precision == "day"


async def test_a_limited_run_takes_the_first_calls_and_leaves_the_day_open(env) -> None:
    """Пробный прогон на части звонков не выдаёт сутки за обработанные."""
    queue, sessions, export = env

    report = await run_day(
        DAY, source=LocalExportSource(export), sessionmaker=sessions, queue=queue, limit=1
    )

    assert report.discovered == 2
    assert report.enqueued == 1
    assert report.limited_to == 1
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM speech_calls")) == 1
        assert await session.scalar(text("SELECT count(*) FROM speech_ingest_cursors")) == 0


async def test_a_limit_that_covers_the_day_is_a_full_run(env) -> None:
    queue, sessions, export = env

    report = await run_day(
        DAY, source=LocalExportSource(export), sessionmaker=sessions, queue=queue, limit=10
    )

    assert report.enqueued == 2
    assert report.limited_to is None
    async with sessions() as session:
        stored = await session.scalar(
            text("SELECT last_processed_day FROM speech_ingest_cursors WHERE source = :s"),
            {"s": CURSOR_SOURCE},
        )
    assert stored == DAY


# --------------------------------------------------------------------------- #
# Регрессии по находкам состязательного ревью 2026-09-09
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Дневная доля: часть новых звонков сразу, остальные — с начала ночи
# --------------------------------------------------------------------------- #

MSK = ZoneInfo("Europe/Moscow")
#: Полдень и поздний вечер по Москве: день и ночь для доли «19 → 7».
#:
#: Год дальний нарочно. Отсрочка сверяется с часами базы (`run_after > now()`),
#: и воркер берёт задание по ним же: дата, оставшаяся в прошлом, превращает
#: «отложено до ночи» в «уже можно брать», и тесты падают с того же вечера.
NOON = datetime(2099, 10, 8, 12, 0, tzinfo=MSK)
EVENING = datetime(2099, 10, 8, 21, 0, tzinfo=MSK)
NIGHT_START = datetime(2099, 10, 8, 19, 0, tzinfo=MSK)


def _share(share: float) -> DayShare:
    return DayShare(share=share, night_from=19, night_until=7)


class _Lots(random.Random):
    """Жребий с заданными исходами — по одному на звонок, в порядке источника."""

    def __init__(self, *outcomes: float) -> None:
        super().__init__()
        self._outcomes = list(outcomes)

    def random(self) -> float:
        return self._outcomes.pop(0)


async def _ingest_jobs(sessions: async_sessionmaker) -> list[dict]:
    async with sessions() as session:
        rows = await session.execute(
            text(
                "SELECT j.priority, j.run_after, j.payload, j.run_after > now() AS held "
                "FROM speech_jobs AS j JOIN speech_calls AS c ON c.id = j.call_id "
                "WHERE j.kind = 'ingest' ORDER BY c.session_id"
            )
        )
        return [dict(row) for row in rows.mappings()]


async def test_by_day_a_share_of_new_calls_starts_at_once_and_the_rest_waits_for_the_night(
    env,
) -> None:
    queue, sessions, export = env

    report = await run_day(
        DAY,
        source=LocalExportSource(export),
        sessionmaker=sessions,
        queue=queue,
        day_share=_share(0.5),
        now=NOON,
        # Первый звонок вытянул «сразу», второй — «ночью».
        rng=_Lots(0.2, 0.7),
    )

    assert (report.enqueued, report.deferred) == (2, 1)
    assert "до ночи отложено 1" in report.describe()
    now_job, night_job = await _ingest_jobs(sessions)
    assert (now_job["priority"], now_job["held"], now_job["payload"]) == (0, False, {})
    # Отложенный ждёт начала ночи и уступает свежим и после неё.
    assert (night_job["priority"], night_job["held"]) == (10, True)
    assert night_job["run_after"] == NIGHT_START
    assert night_job["payload"] == {"not_before": NIGHT_START.isoformat()}

    # Очередь выдаёт только немедленное задание, не раскрывая отложенное.
    fresh = await queue.claim([JobLane.DAILY], "intake-test", kinds=[JobKind.INGEST])
    assert fresh is not None and fresh.priority == 0
    assert await queue.claim([JobLane.DAILY], "intake-test", kinds=[JobKind.INGEST]) is None


async def test_at_night_every_new_call_starts_at_once(env) -> None:
    queue, sessions, export = env

    report = await run_day(
        DAY,
        source=LocalExportSource(export),
        sessionmaker=sessions,
        queue=queue,
        # Доля ноль — днём не шло бы ничего. Ночью она не действует.
        day_share=_share(0.0),
        now=EVENING,
    )

    assert (report.enqueued, report.deferred) == (2, 0)
    assert [(job["priority"], job["held"]) for job in await _ingest_jobs(sessions)] == [
        (0, False),
        (0, False),
    ]


async def test_a_share_of_zero_holds_the_whole_day_and_one_holds_nothing(env) -> None:
    queue, sessions, export = env
    source = LocalExportSource(export)

    held = await run_day(
        DAY,
        source=source,
        sessionmaker=sessions,
        queue=queue,
        day_share=_share(0.0),
        now=NOON,
    )
    assert (held.enqueued, held.deferred) == (2, 2)

    # Повторный прогон жребий не перебрасывает: заведённое задание он не трогает,
    # даже если доля с тех пор стала «всё сразу».
    again = await run_day(
        DAY,
        source=source,
        sessionmaker=sessions,
        queue=queue,
        day_share=_share(1.0),
        now=NOON,
    )
    assert (again.enqueued, again.deferred) == (0, 0)
    assert [job["held"] for job in await _ingest_jobs(sessions)] == [True, True]


async def test_the_priority_of_a_run_puts_a_lesser_source_behind(env) -> None:
    """`--priority`: продажи идут после поддержки, а отложенное — после свежего."""
    queue, sessions, export = env

    await run_day(
        DAY,
        source=LocalExportSource(export),
        sessionmaker=sessions,
        queue=queue,
        priority=20,
        day_share=_share(0.5),
        now=NOON,
        rng=_Lots(0.2, 0.7),
    )

    assert [job["priority"] for job in await _ingest_jobs(sessions)] == [20, 30]


async def test_without_a_share_the_run_behaves_as_before(env) -> None:
    queue, sessions, export = env

    report = await run_day(
        DAY, source=LocalExportSource(export), sessionmaker=sessions, queue=queue
    )

    assert report.deferred == 0
    assert [
        (job["priority"], job["held"], job["payload"]) for job in await _ingest_jobs(sessions)
    ] == [
        (0, False, {}),
        (0, False, {}),
    ]


async def test_repeated_intake_does_not_draw_again_or_replace_deferred_job(env) -> None:
    """Решение о дневной доле принимается один раз: новый rng не нужен для известного задания."""
    queue, sessions, export = env
    await run_day(
        DAY,
        source=LocalExportSource(export),
        sessionmaker=sessions,
        queue=queue,
        day_share=_share(0.5),
        now=NOON,
        rng=_Lots(0.9, 0.1),
    )
    async with sessions() as session:
        before = (
            await session.execute(
                text("SELECT id, priority, run_after, payload FROM speech_jobs ORDER BY id")
            )
        ).all()
    await run_day(
        DAY,
        source=LocalExportSource(export),
        sessionmaker=sessions,
        queue=queue,
        day_share=_share(1),
        priority=20,
        now=NOON,
        rng=_Lots(),
    )
    async with sessions() as session:
        after = (
            await session.execute(
                text("SELECT id, priority, run_after, payload FROM speech_jobs ORDER BY id")
            )
        ).all()
    assert before == after
