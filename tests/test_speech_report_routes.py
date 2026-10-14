"""Отчёт в Excel через API — на настоящем Postgres (SP-604, пять уровней с SP-310).

Звонки заводятся настоящим суточным приёмом: так заодно проверяется, что
классификация оператора доезжает от источника до отчёта узлом дерева.
Классификацию ИИ ставит тест — ровно те колонки, которые пишет этап анализа.

Нужен `SPEECH_POSTGRES_TEST_URL` с применённой схемой.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from openpyxl import load_workbook
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.di.http import create_app
from src.di.ingestion import run_day
from src.di.requests import session_dep
from src.infrastructure.persistence import reports_operations as report_operations
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.infrastructure.reports import calls as report_module
from src.infrastructure.sources.ncc.source import CallRef
from src.presentation.http.auth import Principal, current_principal
from src.setting.config import Settings, get_settings
from tests.speech_db_support import speech_test_database_url

_TEST_DATABASE_URL = speech_test_database_url()
pytestmark = pytest.mark.skipif(
    not _TEST_DATABASE_URL,
    reason="SPEECH_POSTGRES_TEST_URL is required",
)

DAY = date(2026, 9, 14)
PERIOD = {"from": "2026-09-14", "to": "2026-09-14"}
SID_A = "node_domain_1_nauss_0_1778844752_17265503"
SID_B = "node_domain_1_nauss_0_1778844752_17265505"
SID_C = "node_domain_0_nauss_0_1778844752_17266458"
SHPD = ("Физ.лицо", "ШПД", "Консультация")
NEGATIVE = ("Физ.лицо", "Мобильная связь", "Недовольство/негатив")


class _Source:
    name = "test"

    def __init__(self, *refs: CallRef) -> None:
        self._refs = refs

    async def list_calls(self, day: date) -> Sequence[CallRef]:
        return self._refs


@dataclass
class Env:
    client: httpx.AsyncClient
    app: FastAPI
    sessions: async_sessionmaker[AsyncSession]
    ids: dict[str, uuid.UUID]


def _ref(session_id: str, *, login: str, path: tuple[str, ...], started: datetime) -> CallRef:
    return CallRef(
        session_id=session_id,
        audio_locator=session_id,
        day=DAY,
        started_at=started,
        duration_s=120,
        operator_login=login,
        project_hint="Волна",
        project_key="reporting_view:calls_detail",
        disposition_path=path,
    )


async def _clean(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        for table in (
            "speech_audit_events",
            "speech_jobs",
            "speech_calls",
            "speech_ingest_cursors",
            "speech_operators",
            "speech_ncc_projects",
        ):
            await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608
        # Сначала листья: родитель под `RESTRICT` раньше детей не удаляется.
        for level in range(5, 0, -1):
            await session.execute(
                text("DELETE FROM speech_classifier_nodes WHERE level = :level"), {"level": level}
            )


async def _seed(sessions: async_sessionmaker[AsyncSession]) -> dict[str, uuid.UUID]:
    source = _Source(
        # 10:00 и 11:00 по Москве 14-го — в отчёте за 14-е.
        _ref(SID_A, login="operator1", path=SHPD, started=datetime(2026, 9, 14, 7, 0, tzinfo=UTC)),
        _ref(
            SID_B, login="operator2", path=NEGATIVE, started=datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
        ),
        # 23:00 по Москве 13-го: в UTC это те же сутки, что у отчёта, по Москве — нет.
        _ref(SID_C, login="operator1", path=SHPD, started=datetime(2026, 9, 13, 20, 0, tzinfo=UTC)),
    )
    # Дерево пустое: узлы путей операторов заводит сам приём.
    await run_day(DAY, source=source, sessionmaker=sessions, queue=PostgresJobQueue(sessions))

    ids: dict[str, uuid.UUID] = {}
    async with sessions() as session, session.begin():
        node_of_call = text("SELECT operator_node_id FROM speech_calls WHERE session_id = :sid")
        parent_of = text("SELECT parent_id FROM speech_classifier_nodes WHERE id = :id")
        ids["consult"] = await session.scalar(node_of_call, {"sid": SID_A})
        ids["negative"] = await session.scalar(node_of_call, {"sid": SID_B})
        ids["shpd"] = await session.scalar(parent_of, {"id": ids["consult"]})
        ids["fiz"] = await session.scalar(parent_of, {"id": ids["shpd"]})
        # Этап анализа: у звонка A ИИ согласен с оператором, у звонка B остановился на «ШПД».
        await session.execute(
            text(
                "UPDATE speech_calls SET ai_node_id = :node, sentiment = 'negative', "
                "summary = 'Абонент спросил про ШПД.', ai_suggestion = CAST(:suggestion AS jsonb) "
                "WHERE session_id = :sid"
            ),
            {
                "node": ids["consult"],
                "suggestion": json.dumps(["Физ.лицо", "ШПД", "Новая формулировка"]),
                "sid": SID_A,
            },
        )
        await session.execute(
            text(
                "UPDATE speech_calls SET ai_node_id = :node, sentiment = 'positive', "
                "ai_suggestion = CAST(:suggestion AS jsonb), ai_suggestion_node_id = :suggested "
                "WHERE session_id = :sid"
            ),
            {
                "node": ids["shpd"],
                "suggestion": json.dumps(list(SHPD)),
                "suggested": ids["consult"],
                "sid": SID_B,
            },
        )
        ids["operator1"] = await session.scalar(
            text("SELECT id FROM speech_operators WHERE login = 'operator1'")
        )
    return ids


@pytest.fixture()
async def env() -> AsyncIterator[Env]:
    assert _TEST_DATABASE_URL is not None
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="local",
        speech_auth_mode="open",
        speech_ms_database_url=_TEST_DATABASE_URL,
    )
    engine = create_async_engine(_TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[session_dep] = _override

    await _clean(sessions)
    ids = await _seed(sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        yield Env(client=client, app=app, sessions=sessions, ids=ids)
    await _clean(sessions)
    app.dependency_overrides.clear()
    await engine.dispose()


def _calls(response: httpx.Response) -> list[dict[str, Any]]:
    sheet = load_workbook(io.BytesIO(response.content))["Звонки"]
    rows = list(sheet.iter_rows(values_only=True))
    return [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]


def _levels(call: dict[str, Any], side: str) -> list[Any]:
    return [call[f"Уровень {level}: {side}"] for level in range(1, 6)]


async def test_the_period_lists_the_tree_it_uses_with_branch_counts(env: Env) -> None:
    async with env.sessions() as session:
        stored = await session.scalar(
            text("SELECT disposition FROM speech_calls WHERE session_id = :sid"), {"sid": SID_A}
        )
    assert stored == "Физ.лицо › ШПД › Консультация"

    body = (await env.client.get("/api/v1/admin/reports/classification", params=PERIOD)).json()

    assert [
        (item["label"], item["level"], item["operator_calls"], item["ai_calls"])
        for item in body["items"]
    ] == [
        ("Физ.лицо", 1, 2, 2),
        ("Мобильная связь", 2, 1, 0),
        ("Недовольство/негатив", 3, 1, 0),
        ("ШПД", 2, 1, 2),
        ("Консультация", 3, 1, 1),
    ], "звонок 23:00 по Москве 13-го в сутки 14-го не попал; счёт — по ветке, порядок — дерева"


async def test_the_report_lays_both_paths_level_by_level(env: Env) -> None:
    response = await env.client.get("/api/v1/admin/reports/calls.xlsx", params=PERIOD)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert (
        'filename="speech-report-2026-09-14_2026-09-14.xlsx"'
        in response.headers["content-disposition"]
    )
    calls = _calls(response)
    assert [call["Идентификатор сессии"] for call in calls] == [SID_A, SID_B]
    first, second = calls
    assert first["Начало"] == datetime(2026, 9, 14, 10, 0), "местное время колл-центра"
    assert first["Проблема (классификация NCC)"] == "Физ.лицо › ШПД › Консультация"
    assert _levels(first, "оператор") == _levels(first, "ИИ") == [*SHPD, None, None]
    assert (first["Совпало уровней"], first["Совпадение, %"]) == (3, 60), (
        "уровней 4 и 5 у оператора нет — они не совпадают"
    )
    assert (first["Тема по предложению ИИ помощника"], first["Новая формулировка"]) == (
        "Физ.лицо › ШПД › Новая формулировка",
        "да",
    )
    assert _levels(second, "ИИ") == ["Физ.лицо", "ШПД", None, None, None]
    assert (second["Совпало уровней"], second["Совпадение, %"]) == (1, 20)
    assert (second["Тема по предложению ИИ помощника"], second["Новая формулировка"]) == (
        "Физ.лицо › ШПД › Консультация",
        "нет",
    )


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        # Узел отбирает всю свою ветку.
        ({"operator_node_id": "fiz"}, [SID_A, SID_B]),
        ({"operator_node_id": "negative"}, [SID_B]),
        ({"ai_node_id": "shpd"}, [SID_A, SID_B]),
        ({"ai_node_id": "consult"}, [SID_A]),
        ({"operator_node_id": "shpd", "ai_node_id": "shpd"}, [SID_A]),
        ({"operator_id": "operator1"}, [SID_A]),
    ],
)
async def test_filters_narrow_the_report(
    env: Env, filters: dict[str, str], expected: list[str]
) -> None:
    params = {**PERIOD, **{name: str(env.ids[value]) for name, value in filters.items()}}

    response = await env.client.get("/api/v1/admin/reports/calls.xlsx", params=params)

    assert [call["Идентификатор сессии"] for call in _calls(response)] == expected


async def test_each_report_is_an_audit_event_and_names_its_filters(env: Env) -> None:
    response = await env.client.get(
        "/api/v1/admin/reports/calls.xlsx",
        params={**PERIOD, "ai_node_id": str(env.ids["shpd"])},
    )

    async with env.sessions() as session:
        events = (
            (await session.execute(text("SELECT event_type, payload FROM speech_audit_events")))
            .mappings()
            .all()
        )
    assert [event["event_type"] for event in events] == ["report.calls"]
    assert events[0]["payload"]["rows"] == 2
    assert events[0]["payload"]["ai_node_id"] == str(env.ids["shpd"])
    about = {
        row[0]: row[1]
        for row in load_workbook(io.BytesIO(response.content))["Отчёт"].iter_rows(values_only=True)
    }
    assert about["Классификация ИИ (с веткой)"] == "Физ.лицо › ШПД"


async def _transcript(session: AsyncSession, sid: str, *, segments: list[tuple[str, str]]) -> None:
    """Расшифровка звонка: реплики (роль, текст) по порядку."""
    call_id = await session.scalar(
        text("SELECT id FROM speech_calls WHERE session_id = :sid"), {"sid": sid}
    )
    transcript_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO speech_transcripts (id, call_id, engine, model_name, language, "
            "role_method, segments_count, created_at) VALUES (:id, :call, "
            "'whisper', 'large-v3', 'ru', 'stereo_energy', :n, now())"
        ),
        {"id": transcript_id, "call": call_id, "n": len(segments)},
    )
    # В обратном порядке: отчёт обязан собрать реплики по `seq`, а не по вставке.
    for seq, (speaker, raw) in reversed(list(enumerate(segments, start=1))):
        await session.execute(
            text(
                "INSERT INTO speech_transcript_segments (transcript_id, call_id, seq, speaker, "
                "start_ms, end_ms, text_raw) VALUES (:t, :call, :seq, "
                ":speaker, :start, :end, :raw)"
            ),
            {
                "t": transcript_id,
                "call": call_id,
                "seq": seq,
                "speaker": speaker,
                "start": seq * 1000,
                "end": seq * 1000 + 900,
                "raw": raw,
            },
        )


async def test_the_report_carries_the_transcript_as_it_is(env: Env) -> None:
    """S-31: расшифровка в каждом отчёте — тот же текст, что в карточке, без купюр."""
    async with env.sessions() as session, session.begin():
        await _transcript(
            session,
            SID_A,
            segments=[
                ("operator", "Здравствуйте."),
                ("client", "Мой номер 89780001122."),
                ("unknown", "Алло?"),
            ],
        )
        # У звонка B расшифровки ещё нет: он в очереди.

    response = await env.client.get("/api/v1/admin/reports/calls.xlsx", params=PERIOD)

    calls = {call["Идентификатор сессии"]: call for call in _calls(response)}
    assert calls[SID_A]["Расшифровка"] == (
        "Оператор: Здравствуйте.\nКлиент: Мой номер 89780001122.\nНе определено: Алло?"
    )
    assert calls[SID_B]["Расшифровка"] == "[звонок в обработке — расшифровки ещё нет]"
    async with env.sessions() as session:
        payload = await session.scalar(
            text("SELECT payload FROM speech_audit_events WHERE event_type = 'report.calls'")
        )
    assert (payload["transcripts"], payload["transcripts_truncated"]) == (1, 0)


async def test_a_second_report_while_one_is_building_is_refused(env: Env) -> None:
    """Две сборки сразу — вдвое больше памяти и tmpfs; второму — ясный отказ."""
    from src.presentation.http import report_routes as report_routes

    async with report_operations._BUILDING:
        response = await env.client.get("/api/v1/admin/reports/calls.xlsx", params=PERIOD)

    assert response.status_code == 429
    assert response.json()["detail"] == "report_busy"


async def test_an_editor_cannot_take_the_report(env: Env) -> None:
    env.app.dependency_overrides[current_principal] = lambda: Principal(
        sub="editor-1",
        username="editor",
        email=None,
        display_name=None,
        roles=("speech_reader", "speech_editor"),
        auth_mode="keycloak",
    )
    try:
        response = await env.client.get("/api/v1/admin/reports/calls.xlsx", params=PERIOD)
        options = await env.client.get("/api/v1/admin/reports/classification", params=PERIOD)
    finally:
        env.app.dependency_overrides.pop(current_principal)

    assert (response.status_code, options.status_code) == (403, 403)


async def test_a_report_too_large_is_refused_with_a_reason(env: Env, monkeypatch) -> None:
    """Обрезанный отчёт выглядел бы полным — поэтому отказ, а не первые N строк."""
    monkeypatch.setattr(report_module, "REPORT_LIMIT", 1)

    response = await env.client.get("/api/v1/admin/reports/calls.xlsx", params=PERIOD)

    assert response.status_code == 422
    assert response.json()["detail"].startswith("report_too_large")


async def test_an_inverted_period_is_refused(env: Env) -> None:
    response = await env.client.get(
        "/api/v1/admin/reports/calls.xlsx", params={"from": "2026-09-15", "to": "2026-09-14"}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "period_is_empty"
