"""Телфин: разбор метаданных `call_history` и суточный приём.

Разбор — без сети: ответы API подставные, в форме,
снятой разведкой 2026-09-28. Приём — на настоящем Postgres, если задан
`SPEECH_POSTGRES_TEST_URL`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.di.ingestion import run_day
from src.domain.calls.phone import mask_phone_number
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.infrastructure.reports import calls as report
from src.infrastructure.sources.ncc.source import CallRef
from src.infrastructure.sources.telphin.source import (
    PROJECT_KEY,
    TelphinSource,
    client_number,
    day_bounds,
    operator_name,
)
from tests.speech_db_support import speech_test_database_url

DAY = date(2026, 9, 25)
STORAGE = "https://storage.telphin.ru/opaque-token"


def _call(**overrides: Any) -> dict[str, Any]:
    """Вызов `call_history` в форме живого ответа (значения выдуманы)."""
    call: dict[str, Any] = {
        "flow": "out",
        "result": "bridged",
        "init_time_gmt": "2026-09-25 06:04:06",
        "start_time_gmt": "2026-09-25 06:04:32",
        "hangup_time_gmt": "2026-09-25 06:06:24",
        "duration": 112,
        "bridged_duration": 112,
        "extension_id": 1102850,
        "extension_name": "13240*102@sipproxy.telphin.ru",
        "extension_group_owner_id": 19046,
        "from_screen_name": "ГИК Иванова Мария Петровна",
        "to_username": "9161234567",
        "call_uuid": "D9AD938FE52A4987BFF3E71247B89320",
        "cdr": [
            {
                "duration": 112,
                "record_uuid": "1102850-D9AD938FE52A4987BFF3E71247B89320",
                "record_file_size": 227232,
                "storage_url": STORAGE,
            }
        ],
    }
    call.update(overrides)
    return call


def _source(**kwargs: Any) -> TelphinSource:
    defaults: dict[str, Any] = {
        "min_talk_s": 10,
        "excluded_extensions": frozenset({"13240*000", "13240*444"}),
        "excluded_groups": frozenset({22001}),
    }
    return TelphinSource(_Pages(()), **{**defaults, **kwargs})


def test_moscow_day_is_asked_in_utc() -> None:
    assert day_bounds(DAY, "Europe/Moscow") == ("2026-09-24 21:00:00", "2026-09-25 20:59:59")


def test_a_bridged_recorded_call_becomes_a_telphin_call() -> None:
    ref, reason = _source().call_ref(_call(), DAY)
    assert reason is None and ref is not None
    assert ref.session_id == "telphin:D9AD938FE52A4987BFF3E71247B89320"
    assert ref.source == "telphin" and ref.direction == "outbound"
    assert ref.audio_locator == "1102850-D9AD938FE52A4987BFF3E71247B89320"
    assert ref.audio_url == STORAGE
    assert ref.started_at == datetime(2026, 9, 25, 6, 4, 6, tzinfo=UTC)
    assert ref.duration_s == 112
    assert ref.operator_login == "13240*102"
    assert ref.operator_name == "Иванова Мария Петровна"
    assert ref.project_key == PROJECT_KEY
    # Номер без кода страны — к российскому виду: один клиент — один псевдоним.
    assert ref.client_number == "79161234567"


def test_the_link_is_kept_out_of_the_repr() -> None:
    ref, _ = _source().call_ref(_call(), DAY)
    assert STORAGE not in repr(ref)
    assert "9161234567" not in repr(ref)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"result": "not answered", "bridged_duration": 0}, "not_bridged"),
        ({"bridged_duration": 9}, "short"),
        ({"extension_name": "13240*444@sipproxy.telphin.ru"}, "excluded_extension"),
        ({"extension_group_owner_id": 22001}, "excluded_group"),
        ({"cdr": [{"duration": 112, "record_uuid": None, "storage_url": None}]}, "no_record"),
        ({"flow": "in"}, "not_outbound"),
    ],
)
def test_what_is_left_out_and_why(overrides: dict[str, Any], reason: str) -> None:
    ref, why = _source().call_ref(_call(**overrides), DAY)
    assert ref is None and why == reason


def test_a_transferred_call_takes_its_longest_recording() -> None:
    parts = [
        {
            "duration": 20,
            "record_uuid": "short",
            "record_file_size": 1,
            "storage_url": "https://s/1",
        },
        {
            "duration": 90,
            "record_uuid": "long",
            "record_file_size": 2,
            "storage_url": "https://s/2",
        },
    ]
    ref, _ = _source().call_ref(_call(cdr=parts), DAY)
    assert ref is not None and ref.audio_locator == "long" and ref.audio_url == "https://s/2"


def test_number_and_name_helpers() -> None:
    assert client_number("+79161234567") == "+79161234567"
    assert client_number("9161234567") == "79161234567"
    assert client_number(None) is None
    assert operator_name("ГИК Петров Пётр") == "Петров Пётр"
    assert operator_name("B2B") == "B2B"


class _Pages:
    """Клиент API с заготовленными страницами."""

    def __init__(self, pages: Sequence[list[dict[str, Any]]]) -> None:
        self.pages = list(pages)
        self.asked: list[int] = []

    async def call_history(self, *, start: str, end: str, page: int, per_page: int) -> list[Any]:
        self.asked.append(page)
        return self.pages[page - 1] if page <= len(self.pages) else []


async def test_list_calls_reads_pages_until_a_short_one() -> None:
    first = [_call(call_uuid=f"{n:032X}") for n in range(3)]
    second = [_call(call_uuid="F" * 32), _call(call_uuid="E" * 32, bridged_duration=3)]
    client = _Pages([first, second])
    source = _source()
    source._client = client  # type: ignore[assignment]
    source.PER_PAGE = 3  # type: ignore[misc]
    refs = await source.list_calls(DAY)
    assert client.asked == [1, 2]
    assert len(refs) == 4


# --------------------------------------------------------------------------- #
# Приём на живой базе

_TEST_DATABASE_URL = speech_test_database_url()
live = pytest.mark.skipif(not _TEST_DATABASE_URL, reason="SPEECH_POSTGRES_TEST_URL is required")


class _Day:
    name = "test"

    def __init__(self, *refs: CallRef) -> None:
        self._refs = refs

    async def list_calls(self, day: date) -> Sequence[CallRef]:
        return self._refs


Env = tuple[async_sessionmaker[AsyncSession], PostgresJobQueue]


@pytest.fixture()
async def env() -> AsyncIterator[Env]:
    assert _TEST_DATABASE_URL is not None
    engine = create_async_engine(_TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _clean() -> None:
        async with sessions() as session, session.begin():
            for table in (
                "speech_jobs",
                "speech_calls",
                "speech_ingest_cursors",
                "speech_operators",
                "speech_ncc_projects",
            ):
                await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608

    await _clean()
    yield sessions, PostgresJobQueue(sessions, engine=engine)
    await _clean()
    await engine.dispose()


async def _telphin_day(env: Env, *refs: CallRef) -> None:
    sessions, queue = env
    await run_day(
        DAY,
        source=_Day(*refs),
        sessionmaker=sessions,
        queue=queue,
        call_source="telphin",
        phone_pepper="pepper",
    )


async def _one(env: Env, sql: str) -> dict[str, Any]:
    sessions, _ = env
    async with sessions() as session:
        return dict((await session.execute(text(sql))).mappings().one())


async def _link(env: Env) -> str | None:
    link = (await _one(env, "SELECT source_audio_url FROM speech_calls"))["source_audio_url"]
    return None if link is None else str(link)


@live
async def test_a_telphin_call_is_stored_with_its_source_and_without_its_number(env: Env) -> None:
    ref, _ = _source().call_ref(_call(), DAY)
    assert ref is not None
    await _telphin_day(env, ref)
    row = await _one(
        env,
        "SELECT c.source, c.direction, c.source_audio_url, c.client_number_mask,"
        "       c.client_number_hash, c.source_snapshot::text AS snapshot,"
        "       o.login, o.full_name, p.direction AS project_direction,"
        "       (SELECT source FROM speech_ingest_cursors) AS cursor"
        "  FROM speech_calls c"
        "  JOIN speech_operators o ON o.id = c.operator_id"
        "  JOIN speech_ncc_projects p ON p.id = c.project_id",
    )
    assert row["source"] == "telphin" and row["direction"] == "outbound"
    assert row["source_audio_url"] == STORAGE
    assert row["client_number_mask"] == mask_phone_number("79161234567")
    assert row["client_number_hash"] and len(row["client_number_hash"]) == 64
    assert "9161234567" not in row["snapshot"] and STORAGE not in row["snapshot"]
    assert (row["login"], row["full_name"]) == ("13240*102", "Иванова Мария Петровна")
    assert row["project_direction"] == "outbound"
    assert row["cursor"] == "telphin_daily"


@live
async def test_the_link_comes_back_only_while_the_recording_is_not_taken(env: Env) -> None:
    ref, _ = _source().call_ref(_call(), DAY)
    assert ref is not None
    sessions, _ = env
    await _telphin_day(env, ref)

    async with sessions() as session, session.begin():
        await session.execute(text("UPDATE speech_calls SET source_audio_url = NULL"))
    await _telphin_day(env, ref)
    assert await _link(env) == STORAGE

    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_calls SET source_audio_url = NULL, audio_status = 'stored'")
        )
    await _telphin_day(env, ref)
    assert await _link(env) is None


@live
async def test_the_reports_do_not_mix_the_sources(env: Env) -> None:
    ref, _ = _source().call_ref(_call(), DAY)
    assert ref is not None
    sessions, queue = env
    await _telphin_day(env, ref)
    ncc = CallRef(
        session_id="node_domain_1_nauss_0_1778844752_1",
        audio_locator="node_domain_1_nauss_0_1778844752_1",
        day=DAY,
        started_at=datetime(2026, 9, 25, 7, 0, tzinfo=UTC),
    )
    await run_day(DAY, source=_Day(ncc), sessionmaker=sessions, queue=queue)

    period = (datetime(2026, 9, 24, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC))
    async with sessions() as session:
        default = await report.load_report(
            session,
            report.ReportFilters(date_from=period[0], date_to=period[1]),
            limit=report.REPORT_LIMIT,
        )
        telphin = await report.load_report(
            session,
            report.ReportFilters(date_from=period[0], date_to=period[1], source="telphin"),
            limit=report.REPORT_LIMIT,
        )
    assert [row.session_id for row in default] == [ncc.session_id]
    assert [row.session_id for row in telphin] == [ref.session_id]


class _EveryDay:
    """По звонку на каждые сутки — чтобы видеть, какие сутки взяты."""

    name = "test"

    async def list_calls(self, day: date) -> Sequence[CallRef]:
        return [
            CallRef(
                session_id=f"telphin:{day.isoformat()}",
                audio_locator="rec",
                day=day,
                started_at=datetime(day.year, day.month, day.day, 9, tzinfo=UTC),
                source="telphin",
            )
        ]


@live
async def test_the_timer_run_takes_fresh_days_then_history_in_portions(env: Env) -> None:
    from src.di.ingestion import catch_up

    sessions, queue = env

    async def run() -> list[str]:
        return await catch_up(
            source=_EveryDay(),
            sessionmaker=sessions,
            queue=queue,
            call_source="telphin",
            today=date(2026, 9, 10),
            history_from=date(2026, 9, 5),
            history_days=2,
        )

    async def lanes() -> dict[str, str]:
        async with sessions() as session:
            rows = await session.execute(
                text(
                    "SELECT c.session_id, j.lane FROM speech_calls c "
                    "JOIN speech_jobs j ON j.call_id = c.id"
                )
            )
            return {sid.removeprefix("telphin:"): lane for sid, lane in rows}

    first = await run()
    assert any("окно открыто" in line for line in first)
    assert await lanes() == {
        "2026-09-09": "daily",
        "2026-09-10": "daily",
        "2026-09-08": "backfill",
    }, "вчера и сегодня — суточной полосой; история — полосой истории, порцией"

    await run()
    await run()
    last = await run()
    assert sorted(await lanes()) == [f"2026-09-{day:02d}" for day in range(5, 11)]
    assert "завершён" in last[-1]
