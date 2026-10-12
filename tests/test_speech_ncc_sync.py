"""Синхронизация справочников NCC на настоящем Postgres.

База живая, а NCC подставной. Так и надо: проверяется не то, что клиент
вызывается, а что справочник после прогона выглядит определённым образом —
включая случаи, где правильный ответ «ничего не делать» или «не удалять».

Мок на месте базы показал бы, что код зовёт `UPDATE`, и промолчал бы о том, что
`ON CONFLICT ... WHERE` не переписывает неизменившуюся строку, — а именно на
этом и держится честность отчёта.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.infrastructure.sources.ncc.rest import NccEmployee, NccError, NccProject
from src.infrastructure.sources.ncc.sync import sync_employees, sync_projects
from tests.speech_db_support import speech_test_database_url

_TEST_DATABASE_URL = speech_test_database_url()
pytestmark = pytest.mark.skipif(
    not _TEST_DATABASE_URL,
    reason="SPEECH_POSTGRES_TEST_URL is required: справочник проверяется на живой базе",
)


class _Ncc:
    """Подставной NCC: отдаёт заготовленное, считает запросы."""

    def __init__(
        self,
        *,
        active: Sequence[NccEmployee] = (),
        dismissed: Sequence[NccEmployee] = (),
        projects: dict[str, NccProject | NccError] | None = None,
    ) -> None:
        self._active = tuple(active)
        self._dismissed = tuple(dismissed)
        self._projects = projects or {}
        self.employee_calls: list[bool] = []
        self.project_calls: list[str] = []

    async def employees(self, *, removed: bool = False) -> Sequence[NccEmployee]:
        self.employee_calls.append(removed)
        return self._dismissed if removed else self._active

    async def project(self, uuid: str) -> NccProject:
        self.project_calls.append(uuid)
        answer = self._projects.get(uuid)
        if answer is None:
            raise NccError("projects_not_found", retryable=False)
        if isinstance(answer, NccError):
            raise answer
        return answer


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_TEST_DATABASE_URL or "")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM speech_operators"))
        await session.execute(text("DELETE FROM speech_ncc_projects"))
    yield maker
    async with maker() as session, session.begin():
        await session.execute(text("DELETE FROM speech_operators"))
        await session.execute(text("DELETE FROM speech_ncc_projects"))
    await engine.dispose()


async def _operators(maker: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT login, full_name, department, removed, ncc_uuid, "
                    "synced_at IS NOT NULL AS synced "
                    "FROM speech_operators ORDER BY login"
                )
            )
        ).mappings()
        return [dict(row) for row in rows]


async def _add_project(
    maker: async_sessionmaker[AsyncSession],
    *,
    ncc_uuid: str,
    title: str = "Старое имя",
    direction: str = "unknown",
    channel: str | None = None,
) -> None:
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_ncc_projects (id, ncc_uuid, title, data_channel, direction, "
                "ingest_enabled, created_at, updated_at) "
                "VALUES (:id, :uuid, :title, :channel, :direction, TRUE, now(), now())"
            ),
            {
                "id": uuid_module.uuid4(),
                "uuid": ncc_uuid,
                "title": title,
                "channel": channel,
                "direction": direction,
            },
        )


# --------------------------------------------------------------------------- #
# Сотрудники
# --------------------------------------------------------------------------- #


async def test_both_lists_are_requested(sessions: Any) -> None:
    """`removed=true` у NCC означает «только уволенные», а не «включая».

    Спросив один раз, мы получили бы половину справочника и не заметили этого:
    пропавшие выглядели бы уволившимися.
    """
    ncc = _Ncc(active=[NccEmployee(login="a")], dismissed=[NccEmployee(login="b")])

    report = await sync_employees(ncc, sessions)  # type: ignore[arg-type]

    assert ncc.employee_calls == [False, True]
    assert report.seen == 2
    assert [row["login"] for row in await _operators(sessions)] == ["a", "b"]


async def test_a_dismissed_employee_is_marked_not_deleted(sessions: Any) -> None:
    ncc = _Ncc(
        active=[NccEmployee(login="ivanov", display_name="Иванов Иван")],
        dismissed=[NccEmployee(login="petrov", display_name="Петров Пётр", removed=True)],
    )

    await sync_employees(ncc, sessions)  # type: ignore[arg-type]

    rows = {row["login"]: row for row in await _operators(sessions)}
    assert rows["ivanov"]["removed"] is False
    assert rows["petrov"]["removed"] is True
    assert rows["petrov"]["full_name"] == "Петров Пётр"


async def test_someone_who_vanished_from_ncc_keeps_their_row(sessions: Any) -> None:
    """Логин стоит у звонков: удалив строку, мы подписали бы их «оператор не указан».

    Причём задним числом — у разговоров, которые этот человек и провёл.
    """
    await sync_employees(_Ncc(active=[NccEmployee(login="ushel")]), sessions)  # type: ignore[arg-type]

    report = await sync_employees(_Ncc(active=[NccEmployee(login="ostalsya")]), sessions)  # type: ignore[arg-type]

    rows = {row["login"]: row for row in await _operators(sessions)}
    assert set(rows) == {"ushel", "ostalsya"}, "строка пропавшего удалена"
    assert rows["ushel"]["removed"] is True
    assert report.note is not None
    assert "уволенными" in report.note


async def test_an_unchanged_directory_reports_no_changes(sessions: Any) -> None:
    """`ON CONFLICT DO UPDATE` пишет строку всегда — и отчёт врал бы каждый раз.

    «Обновлено 800» на справочнике, где ничего не изменилось, ничего не
    сообщает: по нему нельзя понять, случилось ли что-нибудь.
    """
    ncc = _Ncc(active=[NccEmployee(login="a", display_name="А", department="Отдел")])

    first = await sync_employees(ncc, sessions)  # type: ignore[arg-type]
    second = await sync_employees(ncc, sessions)  # type: ignore[arg-type]

    assert (first.created, first.updated, first.unchanged) == (1, 0, 0)
    assert (second.created, second.updated, second.unchanged) == (0, 0, 1)


async def test_a_changed_field_counts_as_an_update(sessions: Any) -> None:
    await sync_employees(_Ncc(active=[NccEmployee(login="a", display_name="Было")]), sessions)  # type: ignore[arg-type]

    report = await sync_employees(  # type: ignore[arg-type]
        _Ncc(active=[NccEmployee(login="a", display_name="Стало")]), sessions
    )

    assert (report.created, report.updated, report.unchanged) == (0, 1, 0)
    assert (await _operators(sessions))[0]["full_name"] == "Стало"


async def test_an_employee_without_a_name_is_shown_by_login(sessions: Any) -> None:
    """Имя обязательно в таблице, а у NCC может отсутствовать.

    Логин честнее пустой строки: он и есть то, чем оператор опознаётся.
    """
    await sync_employees(_Ncc(active=[NccEmployee(login="nonamed")]), sessions)  # type: ignore[arg-type]

    assert (await _operators(sessions))[0]["full_name"] == "nonamed"


async def test_a_dismissal_wins_over_the_active_list(sessions: Any) -> None:
    """Один и тот же человек в обоих списках — расхождение на стороне NCC.

    Верим более сильному утверждению: уволен. Обратный выбор вернул бы
    уволенного в фильтр действующих операторов.
    """
    ncc = _Ncc(
        active=[NccEmployee(login="oba")],
        dismissed=[NccEmployee(login="oba", removed=True)],
    )

    await sync_employees(ncc, sessions)  # type: ignore[arg-type]

    assert (await _operators(sessions))[0]["removed"] is True


# --------------------------------------------------------------------------- #
# Проекты
# --------------------------------------------------------------------------- #


async def test_an_empty_directory_says_why_it_is_empty(sessions: Any) -> None:
    """«Синхронизировано 0 проектов» на пустой таблице читается как поломка.

    А поломки нет: перечислить проекты в NCC нечем, списочного запроса у REST
    API не существует.
    """
    ncc = _Ncc()

    report = await sync_projects(ncc, sessions)  # type: ignore[arg-type]

    assert report.seen == 0
    assert ncc.project_calls == [], "спрашивали NCC, хотя спрашивать не о чем"
    assert report.note is not None
    assert "списочного запроса" in report.note


async def test_a_known_project_is_refreshed(sessions: Any) -> None:
    await _add_project(sessions, ncc_uuid="p1", title="Старое имя")
    ncc = _Ncc(projects={"p1": NccProject(uuid="p1", title="Поддержка В2В", data_channel="queue")})

    report = await sync_projects(ncc, sessions)  # type: ignore[arg-type]

    assert (report.seen, report.updated, report.unchanged) == (1, 1, 0)
    async with sessions() as session:
        row = (
            (
                await session.execute(
                    text("SELECT title, direction, data_channel FROM speech_ncc_projects")
                )
            )
            .mappings()
            .one()
        )
    assert row["title"] == "Поддержка В2В"
    assert row["direction"] == "inbound"


async def test_an_unchanged_project_is_not_rewritten(sessions: Any) -> None:
    await _add_project(
        sessions, ncc_uuid="p1", title="Очередь", direction="inbound", channel="queue"
    )
    ncc = _Ncc(projects={"p1": NccProject(uuid="p1", title="Очередь", data_channel="queue")})

    report = await sync_projects(ncc, sessions)  # type: ignore[arg-type]

    assert (report.updated, report.unchanged) == (0, 1)


async def test_a_project_deleted_in_ncc_keeps_its_row(sessions: Any) -> None:
    """На строку ссылаются звонки: убрав её, они потеряли бы название очереди."""
    await _add_project(sessions, ncc_uuid="ushel", title="Закрытая очередь")
    ncc = _Ncc(projects={})

    report = await sync_projects(ncc, sessions)  # type: ignore[arg-type]

    assert report.failed == 1
    assert report.note is not None and "сохранены" in report.note
    async with sessions() as session:
        title = await session.scalar(text("SELECT title FROM speech_ncc_projects"))
    assert title == "Закрытая очередь"


async def test_one_broken_project_does_not_stop_the_rest(sessions: Any) -> None:
    """Справочник из сотни очередей не должен вставать из-за одной."""
    await _add_project(sessions, ncc_uuid="a")
    await _add_project(sessions, ncc_uuid="b")
    ncc = _Ncc(
        projects={
            "a": NccError("projects_http_500", retryable=True),
            "b": NccProject(uuid="b", title="Живая", data_channel="queue"),
        }
    )

    report = await sync_projects(ncc, sessions)  # type: ignore[arg-type]

    assert report.failed == 1
    assert report.updated == 1
    assert sorted(ncc.project_calls) == ["a", "b"]


async def test_an_ambiguous_channel_leaves_the_direction_unknown(sessions: Any) -> None:
    """`autoinformer` числится у NCC и входящим, и исходящим.

    Выбрать за источник нельзя: направление видно в карточке звонка, и
    угаданное там неотличимо от известного.
    """
    await _add_project(sessions, ncc_uuid="p1", direction="inbound")
    ncc = _Ncc(
        projects={"p1": NccProject(uuid="p1", title="Автоинформатор", data_channel="autoinformer")}
    )

    await sync_projects(ncc, sessions)  # type: ignore[arg-type]

    async with sessions() as session:
        direction = await session.scalar(text("SELECT direction FROM speech_ncc_projects"))
    assert direction == "unknown"


async def test_the_manual_markup_survives_a_sync(sessions: Any) -> None:
    """Сегмент и продукт проставлены руками в админке — источник их не знает.

    Синхронизация, стирающая их, обесценила бы фильтры выдачи при каждом
    ночном прогоне.
    """
    await _add_project(sessions, ncc_uuid="p1")
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_ncc_projects SET segment = 'b2b', product = 'mobile'")
        )

    await sync_projects(  # type: ignore[arg-type]
        _Ncc(projects={"p1": NccProject(uuid="p1", title="Новое имя", data_channel="queue")}),
        sessions,
    )

    async with sessions() as session:
        row = (
            (await session.execute(text("SELECT segment, product FROM speech_ncc_projects")))
            .mappings()
            .one()
        )
    assert (row["segment"], row["product"]) == ("b2b", "mobile")


async def test_the_ingest_switch_survives_a_sync(sessions: Any) -> None:
    """Выключенный приём — решение администратора, а не свойство проекта в NCC."""
    await _add_project(sessions, ncc_uuid="p1")
    async with sessions() as session, session.begin():
        await session.execute(text("UPDATE speech_ncc_projects SET ingest_enabled = FALSE"))

    await sync_projects(  # type: ignore[arg-type]
        _Ncc(projects={"p1": NccProject(uuid="p1", title="Новое", data_channel="queue")}),
        sessions,
    )

    async with sessions() as session:
        enabled = await session.scalar(text("SELECT ingest_enabled FROM speech_ncc_projects"))
    assert enabled is False


async def test_a_project_known_only_by_its_source_is_not_asked_of_ncc(sessions: Any) -> None:
    """Проект из представления отчётной базы UUID не имеет — спросить о нём NCC нечем.

    Спросив пустое, синхронизация записала бы его в «удалённые в NCC».
    """
    await _add_project(sessions, ncc_uuid="p1", title="Очередь")
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_ncc_projects (id, source_key, title, direction, "
                "ingest_enabled, created_at, updated_at) "
                "VALUES (:id, 'reporting_view:calls_detail', 'Волна', 'unknown', TRUE, "
                "now(), now())"
            ),
            {"id": uuid_module.uuid4()},
        )
    ncc = _Ncc(projects={"p1": NccProject(uuid="p1", title="Очередь", data_channel="queue")})

    report = await sync_projects(ncc, sessions)  # type: ignore[arg-type]

    assert ncc.project_calls == ["p1"]
    assert (report.seen, report.failed) == (1, 0)
    assert report.note is None
