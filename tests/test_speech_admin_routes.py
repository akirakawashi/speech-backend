"""Администрирование: доступ, следы в журнале и то, что менять нельзя.

Матрица прав уже проверила, что у каждой ручки раздела стоит `admin`. Здесь —
то, чего она не видит: что именно каждая ручка делает с данными, какой след
оставляет и где отказывается.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.di.http import create_app
from src.di.requests import session_dep
from src.presentation.http.auth import Principal, current_principal
from src.setting.config import Settings, get_settings
from tests.speech_db_support import speech_test_database_url

_TEST_DATABASE_URL = speech_test_database_url()
NOW = datetime.now(UTC)


def _principal(*roles: str) -> Principal:
    return Principal(
        sub="admin-1",
        username="admin",
        email=None,
        display_name="Админ",
        roles=tuple(roles),
        auth_mode="keycloak",
    )


@pytest.fixture()
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, Any, dict[str, uuid.UUID]]]:
    if not _TEST_DATABASE_URL:
        pytest.skip("SPEECH_POSTGRES_TEST_URL is required for the admin proof")
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

    ids = await _seed(sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as client:
        yield client, app, ids
    await _clean(sessions)
    app.dependency_overrides.clear()
    await engine.dispose()


async def _clean(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(text("DELETE FROM speech_audit_events"))
        await session.execute(text("DELETE FROM speech_jobs"))
        await session.execute(text("DELETE FROM speech_calls"))
        await session.execute(text("DELETE FROM speech_ncc_projects"))
        await session.execute(text("DELETE FROM speech_settings"))
        await session.execute(text("DELETE FROM speech_users"))


async def _seed(sessions: async_sessionmaker[AsyncSession]) -> dict[str, uuid.UUID]:
    await _clean(sessions)
    ids: dict[str, uuid.UUID] = {}
    async with sessions() as session, session.begin():
        ids["project"] = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO speech_ncc_projects (id, ncc_uuid, title, direction, "
                "ingest_enabled, created_at, updated_at) "
                "VALUES (:id, 'ncc-1', 'Очередь 1', 'inbound', true, now(), now())"
            ),
            {"id": ids["project"]},
        )
        ids["call"] = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO speech_calls (id, session_id, project_id, direction, started_at, "
                "ended_at, duration_s, audio_status, processing_status, analysis_status, "
                "source_snapshot, first_seen_at, created_at, updated_at) "
                "VALUES (:id, 'node_1_admin', :proj, 'inbound', :s, :s, 60, 'stored', 'failed', "
                "'pending', '{}', :s, :s, :s)"
            ),
            {"id": ids["call"], "proj": ids["project"], "s": NOW - timedelta(hours=3)},
        )
        ids["dead_job"] = uuid.uuid4()
        ids["running_job"] = uuid.uuid4()
        for job_id, kind, job_status in (
            (ids["dead_job"], "transcribe", "dead"),
            (ids["running_job"], "analyze", "running"),
        ):
            await session.execute(
                text(
                    "INSERT INTO speech_jobs (id, call_id, kind, lane, status, priority, "
                    "attempts, max_attempts, run_after, lease_seconds, payload, created_at, "
                    "last_error_class, last_error_at) "
                    "VALUES (:id, :call, :kind, 'daily', :st, 100, 3, 3, now(), 300, '{}', "
                    ":created, 'provider_error', :err)"
                ),
                {
                    "id": job_id,
                    "call": ids["call"],
                    "kind": kind,
                    "st": job_status,
                    "created": NOW - timedelta(hours=2),
                    "err": NOW - timedelta(hours=1),
                },
            )
        ids["backfill_job"] = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO speech_jobs (id, call_id, kind, lane, status, priority, attempts, "
                "max_attempts, run_after, lease_seconds, payload, created_at) "
                "VALUES (:id, :call, 'ingest', 'backfill', 'queued', 100, 0, 5, now(), 300, "
                "'{}', now())"
            ),
            {"id": ids["backfill_job"], "call": ids["call"]},
        )
    return ids


async def _audit(app: Any) -> list[dict[str, Any]]:
    override = app.dependency_overrides[session_dep]
    async for session in override():
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT event_type, actor_sub, payload FROM speech_audit_events "
                        "ORDER BY occurred_at"
                    )
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]
    return []


# --------------------------------------------------------------------------- #
# Доступ
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/classifier",
        "/api/v1/admin/projects",
        "/api/v1/admin/settings",
        "/api/v1/admin/audit",
        "/api/v1/admin/users",
        "/api/v1/admin/jobs",
    ],
)
async def test_an_editor_is_refused_everywhere_in_the_section(api, path: str) -> None:
    client, app, _ids = api
    app.dependency_overrides[current_principal] = lambda: _principal("speech_editor")

    response = await client.get(path)

    assert response.status_code == 403
    app.dependency_overrides.pop(current_principal)


# --------------------------------------------------------------------------- #
# Справочники NCC
# --------------------------------------------------------------------------- #


async def test_syncing_the_directories_is_audited_under_its_declared_name(api, monkeypatch) -> None:
    """Ручка писала событие `projects.sync`, которого нет среди типов журнала.

    Журнал отказывает незнакомому типу, а отказ приходил после обращения к NCC и
    до коммита: кнопка «Синхронизировать с NCC» падала при каждом нажатии.
    """
    from src.infrastructure.persistence import admin_operations as admin_routes
    from src.infrastructure.sources.ncc.sync import SyncReport  # noqa: PLC0415

    client, app, _ids = api

    async def _employees(_client: object, _session: object) -> SyncReport:
        return SyncReport(scope="сотрудники", seen=2, created=1, updated=1)

    async def _projects(_client: object, _session: object) -> SyncReport:
        return SyncReport(scope="проекты", seen=1, unchanged=1)

    monkeypatch.setattr(admin_routes, "build_ncc_rest_client", lambda _settings: object())
    monkeypatch.setattr(admin_routes, "sync_employees_in", _employees)
    monkeypatch.setattr(admin_routes, "sync_projects_in", _projects)

    response = await client.post("/api/v1/admin/projects/sync")

    assert response.status_code == 200
    assert response.json()["changed"] == 2
    assert [event["event_type"] for event in await _audit(app)] == ["project.sync"]


async def test_a_project_known_by_its_source_is_listed_without_an_ncc_uuid(api) -> None:
    """Проект из представления отчётной базы (SP-206): UUID нет, есть ключ источника."""
    client, app, _ids = api
    override = app.dependency_overrides[session_dep]
    async for session in override():
        await session.execute(
            text(
                "INSERT INTO speech_ncc_projects (id, source_key, title, direction, "
                "ingest_enabled, created_at, updated_at) "
                "VALUES (gen_random_uuid(), 'reporting_view:calls_detail', 'Волна', 'unknown', "
                "true, now(), now())"
            )
        )
        await session.commit()
        break

    items = (await client.get("/api/v1/admin/projects")).json()["items"]

    volna = next(item for item in items if item["title"] == "Волна")
    assert (volna["ncc_uuid"], volna["source_key"]) == (None, "reporting_view:calls_detail")


# --------------------------------------------------------------------------- #
# Проекты
# --------------------------------------------------------------------------- #


async def test_mapping_a_queue_to_a_segment_is_its_own_event(api) -> None:
    """Сопоставление — факт, а не догадка: у сопоставленной очереди модель о
    сегменте даже не спрашивают."""
    client, app, ids = api

    response = await client.patch(
        f"/api/v1/admin/projects/{ids['project']}", json={"segment": "b2b", "product": "wallet"}
    )

    assert response.status_code == 200
    assert response.json()["segment"] == "b2b"
    assert [e["event_type"] for e in await _audit(app)] == ["project.dimensions"]


async def test_toggling_ingest_is_a_different_event(api) -> None:
    client, app, ids = api

    await client.patch(f"/api/v1/admin/projects/{ids['project']}", json={"ingest_enabled": False})

    assert [e["event_type"] for e in await _audit(app)] == ["project.ingest_toggle"]


async def test_an_omitted_field_is_not_a_cleared_field(api) -> None:
    """Половина полей формы приходит незаполненной; трактовать это как стирание
    значило бы терять сопоставление при каждой правке соседнего поля."""
    client, _app, ids = api
    await client.patch(f"/api/v1/admin/projects/{ids['project']}", json={"segment": "individual"})

    await client.patch(f"/api/v1/admin/projects/{ids['project']}", json={"ingest_enabled": False})

    projects = (await client.get("/api/v1/admin/projects")).json()["items"]
    assert projects[0]["segment"] == "individual"


async def test_clearing_is_explicit(api) -> None:
    client, _app, ids = api
    await client.patch(f"/api/v1/admin/projects/{ids['project']}", json={"segment": "individual"})

    await client.patch(f"/api/v1/admin/projects/{ids['project']}", json={"clear_segment": True})

    projects = (await client.get("/api/v1/admin/projects")).json()["items"]
    assert projects[0]["segment"] is None


async def test_already_classified_calls_are_not_rewritten(api) -> None:
    """Отчёт, построенный вчера, сегодня не должен показывать другие числа."""
    client, app, ids = api
    override = app.dependency_overrides[session_dep]
    async for session in override():
        await session.execute(
            text(
                "UPDATE speech_calls SET segment = 'individual', "
                "segment_source = 'llm' WHERE id = :id"
            ),
            {"id": ids["call"]},
        )
        await session.commit()
        break

    await client.patch(f"/api/v1/admin/projects/{ids['project']}", json={"segment": "b2b"})

    async for session in override():
        row = (
            (
                await session.execute(
                    text("SELECT segment, segment_source FROM speech_calls WHERE id = :id"),
                    {"id": ids["call"]},
                )
            )
            .mappings()
            .one()
        )
        break
    assert row["segment"] == "individual" and row["segment_source"] == "llm"


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #


async def test_settings_say_which_values_an_administrator_chose(api) -> None:
    """Иначе интерфейс не отличает «выставлено ровно 180» от «никто не трогал»."""
    client, _app, _ids = api

    before = (await client.get("/api/v1/admin/settings")).json()
    assert before["values"]["retention_days"] == 180
    assert before["overridden"] == []

    await client.put("/api/v1/admin/settings", json={"values": {"retention_days": 90}})

    after = (await client.get("/api/v1/admin/settings")).json()
    assert after["values"]["retention_days"] == 90
    assert after["overridden"] == ["retention_days"]


async def test_an_undeclared_setting_is_refused_whole(api) -> None:
    """Приняв его, интерфейс показывал бы сохранённое значение, которого никто
    не читает."""
    client, _app, _ids = api

    response = await client.put(
        "/api/v1/admin/settings", json={"values": {"retention_days": 90, "made_up": 1}}
    )

    assert response.status_code == 422
    assert "made_up" in response.json()["detail"]
    assert (await client.get("/api/v1/admin/settings")).json()["overridden"] == [], (
        "отклонённый набор не применяется частично"
    )


async def test_a_wrong_type_is_refused(api) -> None:
    """`retention_days` строкой — это срок хранения, который не применится."""
    client, _app, _ids = api

    response = await client.put("/api/v1/admin/settings", json={"values": {"retention_days": "90"}})

    assert response.status_code == 422


async def test_a_non_positive_window_is_refused(api) -> None:
    client, _app, _ids = api

    response = await client.put("/api/v1/admin/settings", json={"values": {"retention_days": 0}})

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("key", "value"),
    [("stereo_operator_channel", 0), ("short_call_threshold_s", 0)],
)
async def test_a_legitimate_zero_is_accepted(api, key: str, value: int) -> None:
    """Ноль законен не везде одинаково, и это знает `settings_store`.

    Нулевой канал оператора — это первый канал, а нулевой порог короткого
    звонка — отключённый порог. Собственная копия правил в ручке отвергала оба:
    две проверки одного и того же расходятся всегда.
    """
    client, _app, _ids = api

    response = await client.put("/api/v1/admin/settings", json={"values": {key: value}})

    assert response.status_code == 200
    assert response.json()["values"][key] == value


async def test_the_limits_are_the_ones_the_reader_enforces(api) -> None:
    """Записать значение, которое чтение потом отвергнет, — худший из исходов:
    администратор видит своё число в интерфейсе, а работает умолчание."""
    client, _app, _ids = api

    response = await client.put(
        "/api/v1/admin/settings", json={"values": {"stereo_operator_channel": 5}}
    )

    assert response.status_code == 422
    assert "out_of_range" in response.json()["detail"]


async def test_a_broken_max_attempts_shape_is_refused(api) -> None:
    """`max_attempts` — словарь по этапам, а не «любой словарь»."""
    client, _app, _ids = api

    response = await client.put(
        "/api/v1/admin/settings", json={"values": {"max_attempts": {"ingest": 3}}}
    )

    assert response.status_code == 422


async def test_a_settings_change_is_audited_by_keys_not_values(api) -> None:
    client, app, _ids = api
    await client.put("/api/v1/admin/settings", json={"values": {"retention_days": 90}})

    events = await _audit(app)

    assert [e["event_type"] for e in events] == ["settings.update"]
    assert events[0]["payload"]["keys"] == {"count": 1}


# --------------------------------------------------------------------------- #
# Конвейер и задания
# --------------------------------------------------------------------------- #


async def test_the_pipeline_snapshot_does_not_fail_when_a_dependency_is_down(api) -> None:
    """Панель открывают именно тогда, когда что-то не отвечает."""
    client, _app, _ids = api

    response = await client.get("/api/v1/admin/pipeline")

    assert response.status_code == 200
    body = response.json()
    assert {row["lane"] for row in body["lanes"]} == {"daily", "backfill"}
    names = {row["name"] for row in body["dependencies"]}
    assert names == {"storage", "whisper", "llm"}
    assert all(row["configured"] is False for row in body["dependencies"]), (
        "в тестовых настройках зависимостей нет, и это должно быть видно как «не настроено»"
    )


async def test_pipeline_monitors_whisper_without_running_transcription(api, monkeypatch) -> None:
    """Панель конвейера делает только GET /models; ключ и параметры ASR ей не нужны."""
    client, app, _ids = api
    settings = app.dependency_overrides[get_settings]()
    settings.speech_whisper_base_url = "http://whisper.test/v1/"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Записать пробу без внешней сети и вернуть готовность сервера модели."""
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )

    response = await client.get("/api/v1/admin/pipeline")

    assert response.status_code == 200
    health = {row["name"]: row for row in response.json()["dependencies"]}["whisper"]
    assert health["configured"] is True and health["reachable"] is True
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "http://whisper.test/v1/models"
    assert "authorization" not in requests[0].headers


async def test_the_pipeline_does_not_show_a_held_call_as_a_stuck_one(api) -> None:
    """Отложенный приёмом до ночи звонок стоит в очереди, но возраста у него нет."""
    client, app, ids = api
    override = app.dependency_overrides[session_dep]
    held_call = uuid.uuid4()
    async for session in override():
        # Свой звонок: у звонка заготовки заняты все три этапа.
        await session.execute(
            text(
                "INSERT INTO speech_calls (id, session_id, direction, started_at, ended_at, "
                "duration_s, audio_status, processing_status, analysis_status, "
                "source_snapshot, first_seen_at, created_at, updated_at) "
                "VALUES (:id, 'node_1_admin_held', 'inbound', now(), now(), 60, 'pending', "
                "'queued', 'pending', '{}', now(), now(), now())"
            ),
            {"id": held_call},
        )
        await session.execute(
            text(
                "INSERT INTO speech_jobs (id, call_id, kind, lane, status, priority, attempts, "
                "max_attempts, run_after, lease_seconds, payload, created_at) "
                "VALUES (gen_random_uuid(), :call, 'ingest', 'daily', 'queued', 10, 0, 5, "
                "now() + interval '2 hours', 300, "
                "jsonb_build_object('not_before', now() + interval '2 hours'), "
                "now() - interval '5 hours')"
            ),
            {"call": held_call},
        )
        await session.commit()

    body = (await client.get("/api/v1/admin/pipeline")).json()

    rows = {(row["lane"], row["status"]): row for row in body["lanes"]}
    held = rows[("daily", "queued")]
    assert (held["count"], held["oldest_seconds"]) == (1, None)
    # У обычного задания возраст считается, как и раньше.
    assert rows[("backfill", "queued")]["oldest_seconds"] is not None


async def _terminal_replay_setup(app: Any, ids: dict[str, uuid.UUID]) -> None:
    """Only retry/reprocess tests: no concurrently active stage on this call."""
    override = app.dependency_overrides[session_dep]
    async for session in override():
        await session.execute(
            text(
                "UPDATE speech_jobs SET status = 'done', finished_at = now(), "
                "locked_by = NULL, locked_at = NULL WHERE call_id = :call_id "
                "AND id != :dead_id"
            ),
            {"call_id": ids["call"], "dead_id": ids["dead_job"]},
        )
        await session.execute(
            text("UPDATE speech_calls SET completed_at = now() WHERE id = :call_id"),
            {"call_id": ids["call"]},
        )
        await session.commit()
        break


async def test_a_dead_job_can_be_retried_and_its_attempts_reset(api) -> None:
    client, app, ids = api
    await _terminal_replay_setup(app, ids)

    response = await client.post(f"/api/v1/admin/jobs/{ids['dead_job']}/retry")

    assert response.status_code == 200
    override = app.dependency_overrides[session_dep]
    async for session in override():
        row = (
            (
                await session.execute(
                    text(
                        "SELECT j.status, j.attempts, j.lease_seconds, j.priority, "
                        "c.processing_status, c.completed_at FROM speech_jobs j "
                        "JOIN speech_calls c ON c.id = j.call_id WHERE j.id = :id"
                    ),
                    {"id": ids["dead_job"]},
                )
            )
            .mappings()
            .one()
        )
        break
    assert row["status"] == "queued"
    assert row["attempts"] == 0, "оставленный счётчик уронил бы задание обратно в dead"
    assert row["processing_status"] == "queued"
    assert row["completed_at"] is None
    assert row["lease_seconds"] == 900
    assert row["priority"] == 0
    assert [e["event_type"] for e in await _audit(app)] == ["job.retry"]


async def test_a_running_job_cannot_be_retried(api) -> None:
    """Иначе рядом с живым исполнителем появится второй на то же задание."""
    client, app, ids = api

    response = await client.post(f"/api/v1/admin/jobs/{ids['running_job']}/retry")

    assert response.status_code == 409
    assert await _audit(app) == []


async def test_reprocess_queues_exactly_one_stage(api) -> None:
    """Следующие этапы поставит воркер: цепочка описана один раз."""
    client, app, ids = api
    await _terminal_replay_setup(app, ids)

    response = await client.post(f"/api/v1/admin/calls/{ids['call']}/reprocess?from=transcribe")

    assert response.status_code == 200
    override = app.dependency_overrides[session_dep]
    async for session in override():
        kinds = [
            row["kind"]
            for row in (
                await session.execute(
                    text("SELECT kind FROM speech_jobs WHERE call_id = :c AND status = 'queued'"),
                    {"c": ids["call"]},
                )
            ).mappings()
        ]
        break
    assert kinds == ["transcribe"], "ровно один этап; завершённый ingest не переигрывается"
    assert [e["event_type"] for e in await _audit(app)] == ["call.reprocess"]


async def test_reprocessing_the_same_stage_twice_is_a_conflict(api) -> None:
    client, app, ids = api
    await _terminal_replay_setup(app, ids)
    first = await client.post(f"/api/v1/admin/calls/{ids['call']}/reprocess?from=transcribe")
    assert first.status_code == 200

    response = await client.post(f"/api/v1/admin/calls/{ids['call']}/reprocess?from=transcribe")

    assert response.status_code == 409
    assert [e["event_type"] for e in await _audit(app)] == ["call.reprocess"]


async def test_retry_refuses_another_active_stage(api) -> None:
    client, app, ids = api

    response = await client.post(f"/api/v1/admin/jobs/{ids['dead_job']}/retry")

    assert response.status_code == 409
    assert await _audit(app) == []


async def test_reprocess_refuses_another_active_stage(api) -> None:
    client, app, ids = api

    response = await client.post(f"/api/v1/admin/calls/{ids['call']}/reprocess?from=transcribe")

    assert response.status_code == 409
    assert await _audit(app) == []


async def test_reprocessing_an_unknown_call_is_a_clean_404(api) -> None:
    client, _app, _ids = api

    response = await client.post(f"/api/v1/admin/calls/{uuid.uuid4()}/reprocess")

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Бэкфилл
# --------------------------------------------------------------------------- #


async def test_pausing_the_backfill_leaves_the_daily_lane_alone(api) -> None:
    client, app, ids = api

    response = await client.post("/api/v1/admin/backfill/pause")

    assert response.json()["changed"] == 1
    override = app.dependency_overrides[session_dep]
    async for session in override():
        rows = {
            str(row["id"]): row["far"]
            for row in (
                await session.execute(
                    text(
                        "SELECT id, run_after > now() + interval '50 years' AS far FROM speech_jobs"
                    )
                )
            ).mappings()
        }
        break
    assert rows[str(ids["backfill_job"])] is True
    assert rows[str(ids["dead_job"])] is False


async def test_a_paused_backfill_comes_back_whole(api) -> None:
    """Удаление означало бы, что возобновление — это заново собранная история."""
    client, _app, _ids = api
    await client.post("/api/v1/admin/backfill/pause")

    response = await client.post("/api/v1/admin/backfill/start")

    assert response.json()["changed"] == 1


async def test_pause_and_start_are_audited(api) -> None:
    client, app, _ids = api
    await client.post("/api/v1/admin/backfill/pause")
    await client.post("/api/v1/admin/backfill/start")

    assert [e["event_type"] for e in await _audit(app)] == ["backfill.pause", "backfill.start"]


# --------------------------------------------------------------------------- #
# Журнал
# --------------------------------------------------------------------------- #


async def test_the_journal_filters_by_event_type(api) -> None:
    """Закрытый набор типов заведён ровно ради того, чтобы этот фильтр работал."""
    client, _app, ids = api
    await client.patch(f"/api/v1/admin/projects/{ids['project']}", json={"segment": "b2b"})
    await client.post("/api/v1/admin/backfill/pause")

    only = (await client.get("/api/v1/admin/audit?event_type=project.dimensions")).json()

    assert only["total"] == 1
    assert only["items"][0]["event_type"] == "project.dimensions"


async def test_the_journal_is_newest_first_and_paged(api) -> None:
    client, _app, _ids = api
    for _ in range(3):
        await client.post("/api/v1/admin/backfill/pause")

    page = (await client.get("/api/v1/admin/audit?limit=2")).json()

    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["items"][0]["occurred_at"] >= page["items"][1]["occurred_at"]
