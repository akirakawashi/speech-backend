"""Классификатор обращений на настоящем Postgres (SP-310).

Дерево из файла владельца, пути операторов из суточного приёма, разовое
пополнение из представлений и экран администратора. Проверяется то, ради чего
всё это: путь оператора — узел дерева, другое написание не плодит узлов, пустой
уровень не сдвигает остальные, а путь, которого в дереве нет, появляется сам.

Нужен `SPEECH_POSTGRES_TEST_URL` с применённой схемой.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.application.catalogues.service import seed_classifier
from src.application.classifier.backfill import backfill_classifier
from src.application.dto.catalogues import ClassifierSeedReport
from src.application.dto.daily_ingest import DailyRunReport
from src.di.http import create_app
from src.di.ingestion import run_day
from src.di.requests import session_dep
from src.infrastructure.data.catalogues import load_classifier
from src.infrastructure.persistence.catalogues import PostgresClassifierCatalogue
from src.infrastructure.persistence.classifier_backfill import PostgresClassifierBackfillRepository
from src.infrastructure.queue.postgres import PostgresJobQueue
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
SID_A = "node_domain_1_nauss_0_1778844752_17265503"
SID_B = "node_domain_1_nauss_0_1778844752_17265505"
SID_C = "node_domain_0_nauss_0_1778844752_17266458"

FILE_PATHS = load_classifier()
FILE_NODES = 477
#: Путь файла со знаком «/» в названии: выгрузка файлами пишет его через «_».
SLASHED = next(path for path in FILE_PATHS if len(path) == 5 and any("/" in v for v in path))
#: Уровня 4 «Заявка» в файле нет; в отчёте TSS Volna за 28–30.08 он встречался.
NEW_PATH = ("Физ.лицо", "ШПД", "Консультация", "Заявка", "Подключение")
#: Так операторы классифицируют «Кошелёк»: без уровня 1.
WALLET = ("", "Кошелек", "Консультация", "Финансы", "Баланс\\ расходы")

_CHAIN_SQL = """
    WITH RECURSIVE chain AS (
        SELECT id, parent_id, level, label FROM speech_classifier_nodes WHERE id = :id
        UNION ALL
        SELECT n.id, n.parent_id, n.level, n.label
          FROM speech_classifier_nodes AS n
          JOIN chain AS c ON n.id = c.parent_id
    )
    SELECT label FROM chain ORDER BY level
"""


def _respelled(path: Sequence[str]) -> tuple[str, ...]:
    """Тот же путь, как его пишет выгрузка файлами: «_» вместо «/», «й» — «и» с краткой."""
    return tuple(unicodedata.normalize("NFD", level.replace("/", "_")) for level in path)


class _Source:
    """Сутки из заготовленных звонков. Скачивание суточному прогону не нужно."""

    name = "test"

    def __init__(self, *refs: CallRef) -> None:
        self._refs = refs

    async def list_calls(self, day: date) -> Sequence[CallRef]:
        return self._refs


class _Views:
    """Представления за сутки: пути с числом звонков и сами звонки."""

    def __init__(
        self,
        classifications: Mapping[tuple[str, ...], int],
        calls: Sequence[CallRef] = (),
    ) -> None:
        self._classifications = classifications
        self._calls = tuple(calls)
        self.days: list[date] = []

    async def list_classifications(self, day: date) -> Mapping[tuple[str, ...], int]:
        self.days.append(day)
        return self._classifications

    async def list_calls(self, day: date) -> Sequence[CallRef]:
        return self._calls


@dataclass
class Env:
    sessions: async_sessionmaker[AsyncSession]
    queue: PostgresJobQueue
    client: httpx.AsyncClient
    app: FastAPI


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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        yield Env(
            sessions=sessions,
            queue=PostgresJobQueue(sessions, engine=engine),
            client=client,
            app=app,
        )
    await _clean(sessions)
    app.dependency_overrides.clear()
    await engine.dispose()


def _ref(session_id: str, path: Sequence[str] = ()) -> CallRef:
    return CallRef(
        session_id=session_id,
        audio_locator=session_id,
        day=DAY,
        operator_login="operator1",
        project_hint="Волна",
        project_key="reporting_view:calls_detail",
        disposition_path=tuple(path),
    )


async def _rows(env: Env, sql: str, **params: Any) -> list[dict[str, Any]]:
    async with env.sessions() as session:
        return [dict(row) for row in (await session.execute(text(sql), params)).mappings()]


async def _execute(env: Env, sql: str, **params: Any) -> None:
    async with env.sessions() as session, session.begin():
        await session.execute(text(sql), params)


async def _count(env: Env) -> int:
    (row,) = await _rows(env, "SELECT count(*) AS n FROM speech_classifier_nodes")
    return int(row["n"])


async def _seed(env: Env, *, apply: bool) -> ClassifierSeedReport:
    async with env.sessions() as session, session.begin():
        return await seed_classifier(PostgresClassifierCatalogue(session), FILE_PATHS, apply=apply)


async def _run(env: Env, *refs: CallRef) -> DailyRunReport:
    return await run_day(DAY, source=_Source(*refs), sessionmaker=env.sessions, queue=env.queue)


async def _node_of_call(env: Env, session_id: str) -> uuid.UUID | None:
    (row,) = await _rows(
        env, "SELECT operator_node_id FROM speech_calls WHERE session_id = :sid", sid=session_id
    )
    node: uuid.UUID | None = row["operator_node_id"]
    return node


async def _path_of_call(env: Env, session_id: str) -> tuple[str, ...]:
    node = await _node_of_call(env, session_id)
    return tuple(row["label"] for row in await _rows(env, _CHAIN_SQL, id=node))


# --------------------------------------------------------------------------- #
# Дерево из файла
# --------------------------------------------------------------------------- #


async def test_the_file_becomes_a_tree_of_five_levels(env: Env) -> None:
    report = await _seed(env, apply=True)

    assert (report.paths, report.nodes, report.created, report.updated) == (
        397,
        FILE_NODES,
        FILE_NODES,
        0,
    )
    levels = await _rows(
        env,
        "SELECT level, count(*) AS nodes FROM speech_classifier_nodes "
        "GROUP BY level ORDER BY level",
    )
    assert [(row["level"], row["nodes"]) for row in levels] == [
        (1, 2),
        (2, 5),
        (3, 10),
        (4, 90),
        (5, 370),
    ]
    assert await _rows(env, "SELECT DISTINCT source FROM speech_classifier_nodes") == [
        {"source": "file"}
    ]
    (broken,) = await _rows(
        env,
        "SELECT count(*) AS n FROM speech_classifier_nodes AS c "
        "JOIN speech_classifier_nodes AS p ON p.id = c.parent_id WHERE c.level <> p.level + 1",
    )
    assert broken["n"] == 0, "уровень узла — на один ниже родителя"


async def test_a_dry_run_writes_nothing_and_a_second_load_changes_nothing(env: Env) -> None:
    dry = await _seed(env, apply=False)
    assert (dry.created, await _count(env)) == (FILE_NODES, 0)

    await _seed(env, apply=True)
    again = await _seed(env, apply=True)

    assert (again.created, again.updated) == (0, 0)
    assert await _count(env) == FILE_NODES


# --------------------------------------------------------------------------- #
# Пути операторов из суточного приёма
# --------------------------------------------------------------------------- #


async def test_an_operator_path_from_the_file_is_linked_without_new_nodes(env: Env) -> None:
    await _seed(env, apply=True)
    path = next(path for path in FILE_PATHS if len(path) == 5)

    report = await _run(env, _ref(SID_A, path))

    assert report.classifier_created == 0
    assert await _path_of_call(env, SID_A) == path


async def test_another_spelling_is_the_same_node(env: Env) -> None:
    await _seed(env, apply=True)

    report = await _run(env, _ref(SID_A, SLASHED), _ref(SID_B, _respelled(SLASHED)))

    assert report.classifier_created == 0, "«_» и разложенная «й» узлов не плодят"
    assert await _node_of_call(env, SID_A) == await _node_of_call(env, SID_B)
    assert await _path_of_call(env, SID_B) == SLASHED, "название — из файла"


async def test_a_path_the_tree_lacks_is_added_from_ncc(env: Env) -> None:
    await _seed(env, apply=True)

    report = await _run(env, _ref(SID_A, NEW_PATH))

    assert report.classifier_created == 2, "уровни 1–3 есть в файле, новых — два"
    assert "новых узлов классификатора 2" in report.describe()
    assert await _path_of_call(env, SID_A) == NEW_PATH
    assert await _rows(
        env,
        "SELECT level, label, source, is_active FROM speech_classifier_nodes "
        "WHERE source = 'ncc' ORDER BY level",
    ) == [
        {"level": 4, "label": "Заявка", "source": "ncc", "is_active": True},
        {"level": 5, "label": "Подключение", "source": "ncc", "is_active": True},
    ]


async def test_an_empty_level_keeps_its_place(env: Env) -> None:
    await _seed(env, apply=True)

    report = await _run(env, _ref(SID_A, WALLET))

    assert report.classifier_created == 5, "пустого уровня 1 в файле нет — ветка новая целиком"
    assert await _path_of_call(env, SID_A) == WALLET
    assert await _rows(
        env, "SELECT label, key FROM speech_classifier_nodes WHERE level = 1 AND source = 'ncc'"
    ) == [{"label": "", "key": ""}]
    assert await _rows(env, "SELECT disposition FROM speech_calls") == [
        {"disposition": "Кошелек › Консультация › Финансы › Баланс\\ расходы"}
    ], "в проблеме звонка пустых уровней нет"


async def test_a_rerun_fills_the_node_and_does_not_relink_it(env: Env) -> None:
    await _seed(env, apply=True)
    await _run(env, _ref(SID_A))
    assert await _node_of_call(env, SID_A) is None

    await _run(env, _ref(SID_A, SLASHED))
    linked = await _node_of_call(env, SID_A)
    await _run(env, _ref(SID_A, NEW_PATH))

    assert linked is not None
    assert await _node_of_call(env, SID_A) == linked, "уже связанный звонок не переписывается"


async def test_the_file_takes_over_a_node_the_ingest_added_first(env: Env) -> None:
    """Приём мог завести путь раньше, чем загрузили файл: узел не двоится."""
    await _run(env, _ref(SID_A, _respelled(SLASHED)))
    before = await _node_of_call(env, SID_A)

    report = await _seed(env, apply=True)

    assert (report.created, report.updated) == (FILE_NODES - 5, 5)
    assert await _count(env) == FILE_NODES
    assert await _node_of_call(env, SID_A) == before
    assert await _path_of_call(env, SID_A) == SLASHED, "название и происхождение — из файла"


async def test_the_file_turns_its_nodes_back_on_and_leaves_ncc_nodes_alone(env: Env) -> None:
    await _seed(env, apply=True)
    await _run(env, _ref(SID_A, NEW_PATH))
    await _execute(env, "UPDATE speech_classifier_nodes SET is_active = FALSE")

    report = await _seed(env, apply=True)

    assert report.updated == FILE_NODES
    assert await _rows(
        env,
        "SELECT source, is_active, count(*) AS n FROM speech_classifier_nodes "
        "GROUP BY source, is_active ORDER BY source",
    ) == [
        {"source": "file", "is_active": True, "n": FILE_NODES},
        {"source": "ncc", "is_active": False, "n": 2},
    ]


# --------------------------------------------------------------------------- #
# Разовое пополнение из представлений
# --------------------------------------------------------------------------- #


async def test_the_backfill_adds_the_operators_paths_and_a_dry_run_only_counts(env: Env) -> None:
    await _seed(env, apply=True)
    views = _Views({_respelled(SLASHED): 3, NEW_PATH: 2, WALLET: 1})
    days = [DAY, date(2026, 9, 15)]

    dry = await backfill_classifier(
        views, days, repository=PostgresClassifierBackfillRepository(env.sessions), apply=False
    )
    assert (dry.days, dry.calls, dry.paths, dry.created) == (2, 12, 3, 7)
    assert await _count(env) == FILE_NODES

    applied = await backfill_classifier(
        views, days, repository=PostgresClassifierBackfillRepository(env.sessions), apply=True
    )
    again = await backfill_classifier(
        views, days, repository=PostgresClassifierBackfillRepository(env.sessions), apply=True
    )

    assert (applied.created, again.created) == (7, 0)
    assert await _count(env) == FILE_NODES + 7
    assert views.days == days * 3


async def test_the_backfill_links_calls_taken_before_the_tree(env: Env) -> None:
    """Места уровней знает только представление: звонок связывается по его строке."""
    await _seed(env, apply=True)
    await _run(env, _ref(SID_A), _ref(SID_B))
    # Прежний приём писал прочерк в проблему уровнем.
    await _execute(
        env,
        "UPDATE speech_calls SET disposition = 'Кошелек › Консультация › Финансы › -' "
        "WHERE session_id = :sid",
        sid=SID_A,
    )
    views = _Views(
        {WALLET: 1, NEW_PATH: 1, SLASHED: 1},
        calls=(_ref(SID_A, WALLET), _ref(SID_B, NEW_PATH), _ref(SID_C, SLASHED)),
    )

    dry = await backfill_classifier(
        views, [DAY], repository=PostgresClassifierBackfillRepository(env.sessions), apply=False
    )
    assert dry.linked == 2, "звонка SID_C в базе нет"
    assert "связалось бы с узлом оператора: 2" in dry.describe()
    assert await _node_of_call(env, SID_A) is None

    applied = await backfill_classifier(
        views, [DAY], repository=PostgresClassifierBackfillRepository(env.sessions), apply=True
    )
    again = await backfill_classifier(
        views, [DAY], repository=PostgresClassifierBackfillRepository(env.sessions), apply=True
    )

    assert (applied.linked, again.linked) == (2, 0), "связанный звонок не переписывается"
    assert await _path_of_call(env, SID_A) == WALLET
    assert await _path_of_call(env, SID_B) == NEW_PATH
    assert await _rows(
        env, "SELECT disposition FROM speech_calls WHERE session_id = :sid", sid=SID_A
    ) == [{"disposition": "Кошелек › Консультация › Финансы › Баланс\\ расходы"}]


# --------------------------------------------------------------------------- #
# Экран администратора
# --------------------------------------------------------------------------- #


async def test_the_admin_sees_the_tree_with_the_operators_calls(env: Env) -> None:
    await _seed(env, apply=True)
    await _run(env, _ref(SID_A, NEW_PATH), _ref(SID_B, NEW_PATH), _ref(SID_C, SLASHED))

    response = await env.client.get("/api/v1/admin/classifier")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == FILE_NODES + 2
    assert [item["level"] for item in items] == sorted(item["level"] for item in items)
    by_id = {item["id"]: item for item in items}
    leaf = by_id[str(await _node_of_call(env, SID_A))]
    assert (leaf["level"], leaf["label"], leaf["source"], leaf["operator_calls"]) == (
        5,
        "Подключение",
        "ncc",
        2,
    )
    parent = by_id[leaf["parent_id"]]
    assert (parent["label"], parent["operator_calls"]) == ("Заявка", 0), (
        "счёт — звонки ровно этого узла, без ветки под ним"
    )


async def test_the_admin_switches_a_node_off_and_on_and_the_audit_sees_both(env: Env) -> None:
    await _seed(env, apply=True)
    (node,) = await _rows(
        env,
        "SELECT id, label FROM speech_classifier_nodes WHERE level = 1 ORDER BY sort_order LIMIT 1",
    )
    url = f"/api/v1/admin/classifier/{node['id']}"

    off = await env.client.patch(url, json={"is_active": False})
    same = await env.client.patch(url, json={"is_active": False})
    on = await env.client.patch(url, json={"is_active": True})
    missing = await env.client.patch(
        f"/api/v1/admin/classifier/{uuid.uuid4()}", json={"is_active": False}
    )

    assert (off.status_code, off.json()["is_active"]) == (200, False)
    assert (same.status_code, same.json()["detail"]) == (422, "nothing_to_change")
    assert (on.status_code, on.json()["is_active"]) == (200, True)
    assert missing.status_code == 404
    events = await _rows(
        env, "SELECT event_type, payload FROM speech_audit_events ORDER BY occurred_at"
    )
    assert [event["event_type"] for event in events] == ["classifier.toggle", "classifier.toggle"]
    assert events[0]["payload"] == {
        "node_id": str(node["id"]),
        "level": 1,
        "label": node["label"],
        "is_active": False,
    }


async def test_an_editor_can_neither_see_nor_switch_the_tree(env: Env) -> None:
    env.app.dependency_overrides[current_principal] = lambda: Principal(
        sub="editor-1",
        username="editor",
        email=None,
        display_name=None,
        roles=("speech_reader", "speech_editor"),
        auth_mode="keycloak",
    )
    try:
        listed = await env.client.get("/api/v1/admin/classifier")
        switched = await env.client.patch(
            f"/api/v1/admin/classifier/{uuid.uuid4()}", json={"is_active": False}
        )
    finally:
        env.app.dependency_overrides.pop(current_principal)

    assert (listed.status_code, switched.status_code) == (403, 403)
