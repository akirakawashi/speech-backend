"""Offline API regressions: real routes/search/audit, stub database results only.

No engine, application lifespan or integration fixtures are used. PostgreSQL
full-text semantics remain covered by the opt-in integration search tests.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.di.requests import install_dependencies, session_dep
from src.infrastructure.persistence import (
    calls_operations,
    classifier as classifier_module,
    quality_scores as quality_scores,
    search as search_module,
)
from src.infrastructure.persistence.classifier import EMPTY_LEVEL_LABEL
from src.presentation.http import media_routes as media_routes
from src.presentation.http.auth import Principal, current_principal
from src.presentation.http.routes import router as search_router

DAY = datetime(2026, 1, 14, 10, tzinfo=UTC)
START = "2026-01-01T00:00:00Z"
OPERATOR = uuid.UUID(int=1)
PROJECT = uuid.UUID(int=2)
NODE = uuid.UUID(int=3)
TEXT_COLUMNS = (
    "session_id",
    "operator",
    "project",
    "sentiment",
    "segment",
    "product",
    "quote",
)


def _rows(count: int) -> list[dict[str, Any]]:
    return [
        {
            "call_id": uuid.UUID(int=count - index),
            "session_id": f"call-{index}",
            "started_at": DAY - timedelta(seconds=index),
            "duration_s": 120,
            "operator_id": OPERATOR,
            "project_id": PROJECT,
            "node_id": NODE,
            "ai_node_id": NODE,
            "operator_login": "Оператор",
            "project_title": "Проект",
            "sentiment": "neutral",
            "audio_status": "stored",
            "processing_status": "done",
            "segment": "b2b",
            "segment_source": "project",
            "product": "mobile",
            "product_source": "project",
            "source": "ncc",
            "masked_quote": "мой «номер» •••",
            "text_raw": "RAW_TEXT_MUST_NOT_BE_EXPORTED",
            "matches_query": True,
        }
        for index in range(count)
    ]


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> _Result:
        return self

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def one(self) -> dict[str, Any]:
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self) -> list[dict[str, Any]]:
        return self.rows


class _SearchSession:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.pages: list[dict[str, Any]] = []
        self.quote_batches: list[list[uuid.UUID]] = []
        self.score_batches: list[list[uuid.UUID]] = []
        self.events: list[dict[str, Any]] = []
        self.commits = 0
        self.reported_total: int | None = None
        #: Название единственного узла классификатора: из него собирается путь ИИ.
        self.node_label = "Тема"
        #: Запросы KPI и вариантов фильтров экрана: (SQL, параметры).
        self.facets: list[tuple[str, dict[str, Any]]] = []

    def _filtered(
        self, params: dict[str, Any], *, match_query: bool = True
    ) -> list[dict[str, Any]]:
        return sorted(
            [
                row
                for row in self.rows
                if (params["date_from"] is None or row["started_at"] >= params["date_from"])
                and (params["date_to"] is None or row["started_at"] < params["date_to"])
                and all(
                    params[name] is None or row[name] == params[name]
                    for name in (
                        "operator_id",
                        "project_id",
                        "node_id",
                        "segment",
                        "product",
                        "source",
                    )
                )
                and (not match_query or not params["query"] or row["matches_query"])
            ],
            key=lambda row: (row["started_at"], row["call_id"]),
            reverse=True,
        )

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        params = params or {}
        if statement is classifier_module.NODES_SQL:
            return _Result(
                [
                    {
                        "id": NODE,
                        "parent_id": None,
                        "level": 1,
                        "label": self.node_label,
                        "sort_order": 0,
                        "is_active": True,
                    }
                ]
            )
        if statement is search_module._BROWSE_SQL or statement is search_module._SEARCH_SQL:
            self.pages.append(dict(params))
            assert len(self.pages) <= 101, "pagination must be bounded"
            # Assert the real SQL still binds every filter; the stub does not
            # purport to execute PostgreSQL full-text expressions.
            for name in (
                "date_from",
                "date_to",
                "operator_id",
                "project_id",
                "node_id",
                "segment",
                "product",
                "source",
            ):
                assert f":{name}" in str(statement)
            rows = self._filtered(params)
            total = self.reported_total if self.reported_total is not None else len(rows)
            period = len(self._filtered(params, match_query=False))
            return _Result(
                [
                    {
                        **row,
                        "total_calls": total,
                        "period_calls": period,
                        "total_matches": len(rows) if params["query"] else 0,
                        "match_count": 1 if params["query"] else 0,
                    }
                    for row in rows[params["offset"] : params["offset"] + params["limit"]]
                ]
            )
        if statement is search_module._QUOTES_SQL:
            self.quote_batches.append(list(params["call_ids"]))
            by_id = {row["call_id"]: row for row in self.rows}
            return _Result(
                [
                    {
                        "call_id": call_id,
                        "seq": 0,
                        "start_ms": 0,
                        "speaker": "client",
                        "quote": by_id[call_id]["masked_quote"],
                    }
                    for call_id in params["call_ids"]
                ]
            )
        if statement is search_module._HIT_SEQS_SQL:
            return _Result([{"call_id": call_id, "seq": 0} for call_id in params["call_ids"]])
        # Балл звонка в выдаче: запрашивается только для источника скрипта, и
        # оценок в этой подставной базе нет.
        if statement is quality_scores._SCORES_SQL:
            self.score_batches.append(list(params["call_ids"]))
            return _Result([])
        if "INSERT INTO speech_audit_events" in str(statement):
            self.events.append({**params, "payload": json.loads(params["payload"])})
            return _Result([])
        if "AS matches, count(*) AS calls" in str(statement):
            count = len(self._filtered(params))
            return _Result([{"matches": count, "calls": count}])
        # KPI, темы и варианты фильтров: здесь проверяется только, что источник
        # доходит до SQL; что он в самом деле сужает счёт, доказывает живая база.
        if statement is calls_operations._STATS_SQL:
            self.facets.append((str(statement), dict(params)))
            return _Result([{"processed": 0, "transcribed": 0, "queued": 0}])
        if statement is calls_operations._AI_NODE_CALLS_SQL or any(
            table in str(statement)
            for table in ("FROM speech_operators AS o", "FROM speech_ncc_projects AS p")
        ):
            self.facets.append((str(statement), dict(params)))
            return _Result([])
        raise AssertionError(f"Unexpected SQL: {statement}")

    async def scalar(self, statement: Any, params: dict[str, Any]) -> int:
        assert "SELECT count(*) FROM speech_calls" in str(statement)
        return len(self._filtered(params, match_query=False))

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture()
async def export_api() -> AsyncIterator[SimpleNamespace]:
    session = _SearchSession()
    app = FastAPI()
    install_dependencies(app)
    app.include_router(media_routes.router)
    app.include_router(search_router)
    app.dependency_overrides[session_dep] = lambda: session
    app.dependency_overrides[current_principal] = lambda: Principal(
        sub="editor",
        username="editor",
        email=None,
        display_name=None,
        roles=("speech_editor",),
        auth_mode="keycloak",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://speech"
    ) as client:
        yield SimpleNamespace(client=client, app=app, session=session)


def _csv(response: httpx.Response, filename: str = "speech-export.csv") -> list[dict[str, str]]:
    assert response.status_code == 200, response.text
    assert response.text.startswith(media_routes.CSV_BOM)
    assert response.headers["content-disposition"] == f'attachment; filename="{filename}"'
    assert "text/csv" in response.headers["content-type"]
    return list(
        csv.DictReader(
            io.StringIO(response.text.removeprefix(media_routes.CSV_BOM)),
            delimiter=media_routes.CSV_DELIMITER,
        )
    )


def _assert_audit(session: _SearchSession, count: int, query: str, source: str = "ncc") -> None:
    assert session.commits == 1
    assert len(session.events) == 1
    assert session.events[0]["event_type"] == "export.list"
    assert session.events[0]["payload"] == {
        "rows": {"count": count},
        "query_len": len(query.strip()),
        "mode": search_module.parse_mode(query),
        "source": source,
    }


@pytest.mark.parametrize("query", ["", "  номер  ", '"номер"'])
async def test_export_paginates_the_real_search_with_all_filters(export_api, query: str) -> None:
    matching = _rows(257)
    exclusions = [
        {"date": DAY - timedelta(days=2)},
        {"date": DAY + timedelta(hours=1)},
        {"operator_id": uuid.UUID(int=4)},
        {"project_id": uuid.UUID(int=5)},
        {"node_id": uuid.UUID(int=6)},
        {"segment": "individual"},
        {"product": "wallet"},
        {"source": "telphin"},
    ]
    if query.strip():
        exclusions.append({"matches_query": False})
    noise = []
    for index, changed in enumerate(exclusions):
        changed = dict(changed)
        if "date" in changed:
            changed["started_at"] = changed.pop("date")
        noise.append(
            {
                **matching[0],
                **changed,
                "call_id": uuid.UUID(int=1000 + index),
                "session_id": f"excluded-{index}",
            }
        )
    export_api.session.rows = matching + noise
    params = {
        "q": query,
        "from": (DAY - timedelta(days=1)).isoformat(),
        "to": (DAY + timedelta(hours=1)).isoformat(),
        "operator_id": str(OPERATOR),
        "project_id": str(PROJECT),
        "node_id": str(NODE),
        "segment": "b2b",
        "product": "mobile",
        "source": "ncc",
    }

    response = await export_api.client.get("/api/v1/export", params=params)
    rows = _csv(response)

    assert len(rows) == 257
    assert [row["session_id"] for row in rows] == [row["session_id"] for row in matching]
    assert [row["link"] for row in rows] == [f"/calls/{row['call_id']}" for row in matching]
    assert {row["segment"] for row in rows} == {"b2b"}
    assert {row["product"] for row in rows} == {"mobile"}
    assert {row["quote"] for row in rows} == ({"мой «номер» •••"} if query.strip() else {""})
    assert {row["classification"] for row in rows} == {"Тема"}
    assert "RAW_TEXT_MUST_NOT_BE_EXPORTED" not in response.text
    assert [page["offset"] for page in export_api.session.pages] == [0, 100, 200]
    assert all(page["limit"] == 100 for page in export_api.session.pages)
    expected = {
        "query": query.strip(),
        "date_from": DAY - timedelta(days=1),
        "date_to": DAY + timedelta(hours=1),
        "operator_id": OPERATOR,
        "project_id": PROJECT,
        "node_id": NODE,
        "segment": "b2b",
        "product": "mobile",
        "source": "ncc",
    }
    for page in export_api.session.pages:
        assert {key: page[key] for key in expected} == expected
    if query.strip():
        assert [len(batch) for batch in export_api.session.quote_batches] == [100, 100, 57]
    _assert_audit(export_api.session, 257, query)


@pytest.mark.parametrize("count", [0, 1, 100, 101, 10_000, 10_001])
async def test_export_is_bounded_and_audits_the_actual_row_count(export_api, count: int) -> None:
    export_api.session.rows = _rows(count)
    response = await export_api.client.get("/api/v1/export", params={"from": START})
    rows = _csv(response)
    expected = min(count, 10_000)
    assert len(rows) == expected
    assert [row["session_id"] for row in rows] == [f"call-{index}" for index in range(expected)]
    assert len(export_api.session.pages) <= max(1, (expected + 99) // 100)
    assert all(page["limit"] <= 100 for page in export_api.session.pages)
    _assert_audit(export_api.session, expected, "")


@pytest.mark.parametrize("count", [100, 101])
async def test_export_stops_on_an_empty_or_short_page_even_with_stale_totals(export_api, count):
    export_api.session.rows = _rows(count)
    export_api.session.reported_total = 50_000
    response = await export_api.client.get("/api/v1/export", params={"from": START})
    assert len(_csv(response)) == count
    assert [page["offset"] for page in export_api.session.pages] == [0, 100]
    _assert_audit(export_api.session, count, "")


async def test_export_requests_only_the_remaining_capacity(export_api, monkeypatch) -> None:
    monkeypatch.setattr("src.application.media.service.EXPORT_LIMIT", 205)
    export_api.session.rows = _rows(300)
    response = await export_api.client.get("/api/v1/export", params={"from": START})
    assert len(_csv(response)) == 205
    assert [(page["offset"], page["limit"]) for page in export_api.session.pages] == [
        (0, 100),
        (100, 100),
        (200, 5),
    ]
    _assert_audit(export_api.session, 205, "")


@pytest.mark.parametrize("name", ["segment", "product"])
async def test_export_validates_dimension_filters_like_search(export_api, name: str) -> None:
    response = await export_api.client.get("/api/v1/export", params={name: "not-a-dimension"})
    assert response.status_code == 422
    assert export_api.session.pages == []
    assert export_api.session.events == []


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ("=1+1", True),
        ("+1", True),
        ("-1", True),
        ("@SUM(1,2)", True),
        ("\t=1", True),
        ("\r=1", True),
        ("\n=1", True),
        ("  =1", True),
        (" \t+1", True),
        ("\tplain", True),
        ("", False),
        ("Обычный текст", False),
        ('текст; "цитата"\r\nстрока', False),
        ("123", False),
        ("x=1", False),
        ("'=1", False),
    ],
)
async def test_export_neutralizes_formulas_in_every_text_column(export_api, value, escaped):
    row = _rows(1)[0]
    for field in (
        "session_id",
        "operator_login",
        "project_title",
        "sentiment",
        "segment",
        "product",
        "masked_quote",
    ):
        row[field] = value
    export_api.session.rows = [row]
    # Классификация — путь из названий узлов; пустое название — пустой уровень.
    export_api.session.node_label = value
    response = await export_api.client.get("/api/v1/export", params={"q": "номер", "from": START})
    (exported,) = _csv(response)
    assert {exported[column] for column in TEXT_COLUMNS} == {"'" + value if escaped else value}
    caption = value or EMPTY_LEVEL_LABEL
    assert exported["classification"] == ("'" + caption if escaped else caption)
    assert exported["duration_s"] == "120"
    assert exported["matches"] == "1"
    assert exported["started_at"] == DAY.isoformat()
    assert exported["link"] == f"/calls/{row['call_id']}"


async def test_export_does_not_raise_the_public_search_page_cap(export_api) -> None:
    export_api.session.rows = _rows(257)
    response = await export_api.client.get(
        "/api/v1/search", params={"size": 100, "page": 2, "from": START}
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 100
    assert response.json()["items"][0]["session_id"] == "call-100"
    response = await export_api.client.get("/api/v1/search", params={"size": 101})
    assert response.status_code == 422
    assert len(export_api.session.pages) == 1
    assert export_api.session.events == []


async def test_export_refuses_a_reader_before_search_or_audit(export_api) -> None:
    export_api.app.dependency_overrides[current_principal] = lambda: Principal(
        sub="reader",
        username="reader",
        email=None,
        display_name=None,
        roles=("speech_reader",),
        auth_mode="keycloak",
    )
    response = await export_api.client.get("/api/v1/export")
    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden"
    assert export_api.session.pages == []
    assert export_api.session.events == []
    assert export_api.session.commits == 0


def _two_sources() -> list[dict[str, Any]]:
    """Три звонка NCC и два Телфина за один период."""
    ncc = _rows(3)
    telphin = [
        {**row, "call_id": uuid.UUID(int=500 + index), "session_id": f"telphin-{index}"}
        for index, row in enumerate(_rows(2))
    ]
    for row in telphin:
        row["source"] = "telphin"
    return ncc + telphin


async def test_the_dialogs_list_does_not_mix_the_sources_and_defaults_to_ncc(export_api) -> None:
    """S-35: вкладки «Диалоги» — разные списки, и доля считается внутри источника.

    Без параметра — только NCC: экран, ещё не знающий о `source`, видит то же,
    что видел до прихода Телфина, а не два потока вперемешку.
    """
    export_api.session.rows = _two_sources()

    default = (await export_api.client.get("/api/v1/search", params={"from": START})).json()
    telphin = (
        await export_api.client.get("/api/v1/search", params={"from": START, "source": "telphin"})
    ).json()

    assert [item["session_id"] for item in default["items"]] == ["call-0", "call-1", "call-2"]
    assert {item["source"] for item in default["items"]} == {"ncc"}
    assert default["period_calls"] == 3
    assert [item["session_id"] for item in telphin["items"]] == ["telphin-0", "telphin-1"]
    assert {item["source"] for item in telphin["items"]} == {"telphin"}
    assert (telphin["total_calls"], telphin["period_calls"], telphin["share"]) == (2, 2, 1.0)
    assert [page["source"] for page in export_api.session.pages] == ["ncc", "telphin"]
    # Балл спрашивают только у звонков, которые оцениваются: поиск по NCC за
    # ним в базу не ходит.
    assert export_api.session.score_batches == [[row["call_id"] for row in _two_sources()[3:]]]


async def test_the_export_takes_the_source_of_the_tab(export_api) -> None:
    """Выгрузка — тот же поиск, что на экране, вместе с источником (SRC-09, S-35)."""
    export_api.session.rows = _two_sources()

    response = await export_api.client.get(
        "/api/v1/export", params={"from": START, "source": "telphin"}
    )
    rows = _csv(response, "speech-export-telphin.csv")

    assert [row["session_id"] for row in rows] == ["telphin-0", "telphin-1"]
    _assert_audit(export_api.session, 2, "", source="telphin")


@pytest.mark.parametrize("path", ["/api/v1/stats", "/api/v1/operators", "/api/v1/projects"])
async def test_kpi_and_filter_options_follow_the_source(export_api, path: str) -> None:
    """KPI, темы и варианты «оператор»/«очередь» — того же источника, что и список.

    Оператор Телфина в списке вкладки NCC — выбор, который ничего не найдёт.
    """
    for source in (None, "telphin"):
        params = {"source": source} if source else {}
        assert (await export_api.client.get(path, params=params)).status_code == 200
    assert export_api.session.facets
    assert all("c.source = :source" in sql for sql, _ in export_api.session.facets)
    half = len(export_api.session.facets) // 2
    assert [params["source"] for _, params in export_api.session.facets] == (
        ["ncc"] * half + ["telphin"] * half
    )


@pytest.mark.parametrize(
    "path",
    ["/api/v1/search", "/api/v1/export", "/api/v1/stats", "/api/v1/operators", "/api/v1/projects"],
)
async def test_an_unknown_source_is_refused_before_any_query(export_api, path: str) -> None:
    """Закрытый набор, как у отчёта: опечатка — отказ, а не выдача без фильтра."""
    response = await export_api.client.get(path, params={"source": "all"})

    assert response.status_code == 422
    assert export_api.session.pages == []
    assert export_api.session.facets == []
    assert export_api.session.events == []
