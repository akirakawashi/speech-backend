"""Offline result SQL and opt-in, isolated PostgreSQL pipeline replay proofs.

SQLite executes the real portable result-reset SQL, not locking/queue SQL.
PostgreSQL tests use a fresh random schema, never clean existing tables, and
skip without SPEECH_POSTGRES_TEST_URL. No external speech services are used.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.admin.service import AdminService
from src.application.dto.admin_operations import ReprocessCallQuery, RetryJobQuery
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.queue import NewJob, ReprocessingCallNotFound, ReprocessingConflict
from src.domain.calls.contracts import (
    JobKind,
    JobLane,
    RoleMethod,
)
from src.infrastructure.persistence import (
    admin_operations as admin_routes,
    reprocessing as reprocessing,
)
from src.infrastructure.persistence.admin_operations import PostgresAdminRepository
from src.infrastructure.persistence.base import SpeechBase
from src.infrastructure.persistence.models.speech import (
    SpeechCall,
    SpeechJob,
    SpeechSetting,
    SpeechTopic,
    SpeechTranscript,
    SpeechTranscriptSegment,
)
from src.infrastructure.queue import postgres as postgres
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.presentation.http.auth import Principal
from src.setting.config import Settings

CALL_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()
ATTEMPTS = {kind.value: index + 7 for index, kind in enumerate(JobKind)}
ADMIN = Principal(
    sub="replay-test",
    username="replay-test",
    email=None,
    display_name=None,
    roles=("speech_admin",),
    auth_mode="keycloak",
)


def _new_job(kind: JobKind = JobKind.TRANSCRIBE) -> NewJob:
    return NewJob(
        call_id=CALL_ID,
        kind=kind,
        lane=JobLane.DAILY,
        max_attempts=ATTEMPTS[kind.value],
        lease_seconds=900 if kind is JobKind.TRANSCRIBE else 300,
    )


def _queue_session(rows):
    result = Mock()
    result.mappings.return_value.all.return_value = rows
    session = Mock()
    session.scalar = AsyncMock(side_effect=[CALL_ID, JOB_ID])
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    factory = Mock(side_effect=AssertionError("must use the caller's session"))
    return PostgresJobQueue(factory), session


def _steps(session):
    """Запросы `execute` без начальной установки потолка ожидания.

    `reprocess` начинается с `SET LOCAL lock_timeout` — это преамбула, а не шаг
    работы. Порядок здесь существенный: поставить потолок после того, как запрос
    уже встал в очередь за чужой блокировкой, поздно. Поэтому преамбула
    проверяется один раз и здесь, а не пересчитывается индексами в каждом тесте.
    """
    calls = session.execute.await_args_list
    assert calls, "не выполнено ни одного запроса"
    assert calls[0].args[0] is postgres._SET_CALL_LOCK_TIMEOUT_SQL, (
        "потолок ожидания ставится до захвата блокировок, иначе он ничего не ограничивает"
    )
    return calls[1:]


@pytest.mark.parametrize("kind", list(JobKind))
@pytest.mark.parametrize("status", ["done", "failed", "dead"])
async def test_queue_resets_terminal_id_and_deletes_only_downstream(kind, status):
    queue, session = _queue_session([{"id": JOB_ID, "kind": kind.value, "status": status}])

    assert await queue.reprocess(session, _new_job(kind)) == JOB_ID

    assert session.scalar.await_args_list[0].args[0] is postgres._LOCK_CALL_SQL
    assert _steps(session)[0].args[0] is postgres._LOCK_CALL_JOBS_SQL
    reset, params = session.scalar.await_args_list[-1].args
    assert reset is postgres._RESET_JOB_SQL
    assert params["job_id"] == JOB_ID
    assert params["max_attempts"] == ATTEMPTS[kind.value]
    downstream = list(JobKind)[list(JobKind).index(kind) + 1 :]
    if downstream:
        statement, deleted = _steps(session)[1].args
        assert statement is postgres._DELETE_DOWNSTREAM_SQL
        assert deleted == {"call_id": CALL_ID, "kinds": [k.value for k in downstream]}
        assert "status IN ('done', 'failed', 'dead')" in str(statement)
    else:
        assert len(_steps(session)) == 1
    session.commit.assert_not_called()


@pytest.mark.parametrize("active_kind", list(JobKind))
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_any_active_stage_blocks_queue_mutations(active_kind, status):
    queue, session = _queue_session([{"id": JOB_ID, "kind": active_kind.value, "status": status}])

    with pytest.raises(ReprocessingConflict, match="call_has_queued_or_running_jobs"):
        await queue.reprocess(session, _new_job())

    assert session.scalar.await_count == len(_steps(session)) == 1


async def test_queue_inserts_missing_stage_via_existing_enqueue():
    queue, session = _queue_session([])
    queue.enqueue = AsyncMock(return_value=JOB_ID)
    job = _new_job()

    assert await queue.reprocess(session, job) == JOB_ID

    queue.enqueue.assert_awaited_once_with(session, job)


@pytest.mark.parametrize("status,selected_id", [("done", JOB_ID), ("dead", uuid.uuid4())])
async def test_retry_checks_terminal_status_and_exact_id_under_locks(status, selected_id):
    queue, session = _queue_session([{"id": selected_id, "kind": "transcribe", "status": status}])

    with pytest.raises(ReprocessingConflict, match="job_is_not_failed_or_dead"):
        await queue.reprocess(session, _new_job(), retry_job_id=JOB_ID)

    assert session.scalar.await_count == len(_steps(session)) == 1


async def test_missing_call_never_touches_jobs():
    queue, session = _queue_session([])
    session.scalar.side_effect = [None]
    with pytest.raises(ReprocessingCallNotFound):
        await queue.reprocess(session, _new_job())
    assert not _steps(session), "звонка нет — заданий никто не трогает"


def _busy(message):
    original = RuntimeError("busy")
    # 55P03 = lock_not_available: так отвечает и `NOWAIT`, и истёкший
    # `lock_timeout`. Снаружи это одно и то же — конвейер занят.
    original.sqlstate = "55P03"
    return DBAPIError(message, {}, original)


async def test_job_lock_conflict_is_not_a_deadlock_or_server_error():
    queue, session = _queue_session([])
    result = session.execute.return_value
    session.execute.side_effect = [result, _busy("lock jobs")]
    with pytest.raises(ReprocessingConflict, match="pipeline_is_busy"):
        await queue.reprocess(session, _new_job())
    assert "NOWAIT" in str(postgres._LOCK_CALL_JOBS_SQL)


async def test_a_busy_call_row_is_a_conflict_not_a_server_error():
    """Истёкший потолок ожидания — такой же отказ, как запрет ожидания.

    Без этой ветки `lock_timeout` превратил бы зависание в пятисотую ошибку:
    администратор вместо «попробуйте позже» получил бы «что-то сломалось».
    """
    queue, session = _queue_session([])
    session.scalar.side_effect = _busy("lock call")

    with pytest.raises(ReprocessingConflict, match="pipeline_is_busy"):
        await queue.reprocess(session, _new_job())

    assert not _steps(session), "до заданий дело не дошло"


async def test_an_unrelated_database_error_is_not_disguised_as_busy():
    """Отказ по любой другой причине обязан остаться отказом по своей причине."""
    queue, session = _queue_session([])
    broken = RuntimeError("no such table")
    broken.sqlstate = "42P01"
    session.scalar.side_effect = DBAPIError("lock call", {}, broken)

    with pytest.raises(DBAPIError):
        await queue.reprocess(session, _new_job())


@pytest.mark.parametrize("status", ["failed", "dead"])
async def test_queue_retry_accepts_the_original_failed_id(status):
    queue, session = _queue_session([{"id": JOB_ID, "kind": "transcribe", "status": status}])
    assert await queue.reprocess(session, _new_job(), retry_job_id=JOB_ID) == JOB_ID


@pytest.mark.parametrize("action", ["retry", "reprocess"])
async def test_route_conflict_rolls_back_without_an_audit(monkeypatch, action):
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(admin_routes, "record", audit)
    monkeypatch.setattr(
        admin_routes,
        action,
        AsyncMock(side_effect=ReprocessingConflict("call_has_queued_or_running_jobs")),
    )
    with pytest.raises(ApplicationError) as error:
        if action == "retry":
            await AdminService(
                PostgresAdminRepository(session, Settings(_env_file=None))
            ).retry_job(RetryJobQuery(job_id=JOB_ID, actor=ADMIN))
        else:
            await AdminService(
                PostgresAdminRepository(session, Settings(_env_file=None))
            ).reprocess_call(
                ReprocessCallQuery(call_id=CALL_ID, actor=ADMIN, from_kind=JobKind.ANALYZE)
            )
    assert error.value.kind is ErrorKind.CONFLICT
    audit.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


class _SqliteSession:
    """Execute unchanged, portable result SQL; no claims about PostgreSQL locks."""

    bind = None

    def __init__(self, connection):
        self.connection = connection

    async def execute(self, statement, params=None):
        return self.connection.execute(
            statement,
            {
                key: str(value) if isinstance(value, uuid.UUID) else value
                for key, value in (params or {}).items()
            },
        )


@pytest.fixture()
def result_db():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _functions(connection, _record):
        connection.create_function("now", 0, lambda: "2026-09-10")
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.connect() as connection:
        for ddl in (
            "CREATE TABLE speech_calls (id TEXT PRIMARY KEY, processing_status TEXT, "
            "completed_at TEXT, analysis_status TEXT, topic_id TEXT, sentiment TEXT, "
            "summary TEXT, segment TEXT, segment_source TEXT, product TEXT, "
            "product_source TEXT, updated_at TEXT, ai_topic_id TEXT, ai_topic_label TEXT, "
            "ai_node_id TEXT, ai_suggestion TEXT, ai_suggestion_node_id TEXT, "
            "call_reason TEXT, call_reason_code TEXT)",
            "CREATE TABLE speech_transcripts (id TEXT PRIMARY KEY, call_id TEXT, role_method TEXT)",
            "CREATE TABLE speech_transcript_segments (id INTEGER PRIMARY KEY, "
            "transcript_id TEXT REFERENCES speech_transcripts(id) ON DELETE CASCADE, "
            "call_id TEXT, speaker TEXT, text_raw TEXT)",
            "CREATE TABLE speech_call_evaluations (id TEXT PRIMARY KEY, call_id TEXT)",
        ):
            connection.execute(text(ddl))
        connection.execute(
            text(
                "INSERT INTO speech_calls VALUES (:id, 'done', 'yesterday', 'done', 'topic', "
                "'negative', 'old summary', 'b2b', 'project', 'mobile', 'llm', 'yesterday', "
                "NULL, 'old ai topic', 'old node', '[\"old\"]', 'old suggestion node', "
                "'old reason', 'K05')"
            ),
            {"id": str(CALL_ID)},
        )
        connection.execute(
            text("INSERT INTO speech_transcripts VALUES ('transcript', :id, 'llm_attribution')"),
            {"id": str(CALL_ID)},
        )
        connection.execute(
            text(
                "INSERT INTO speech_transcript_segments VALUES (1, 'transcript', :id, "
                "'client', 'raw preserved')"
            ),
            {"id": str(CALL_ID)},
        )
        connection.execute(
            text("INSERT INTO speech_call_evaluations VALUES ('evaluation', :id)"),
            {"id": str(CALL_ID)},
        )
        connection.commit()
        yield _SqliteSession(connection)
    engine.dispose()


@pytest.mark.parametrize("kind", list(JobKind))
@pytest.mark.parametrize("method", list(RoleMethod))
async def test_actual_result_sql_invalidates_only_derived_outputs(
    result_db, monkeypatch, kind, method
):
    session = result_db
    connection = session.connection
    connection.execute(
        text("UPDATE speech_transcripts SET role_method = :method"), {"method": method.value}
    )
    reset = AsyncMock(return_value=JOB_ID)
    read = AsyncMock(return_value=ATTEMPTS)
    monkeypatch.setattr(PostgresJobQueue, "reprocess", reset)
    monkeypatch.setattr(reprocessing, "read_setting", read)

    before = {
        table: connection.execute(text(f"SELECT * FROM {table}")).mappings().all()
        for table in (
            "speech_calls",
            "speech_transcripts",
            "speech_transcript_segments",
            "speech_call_evaluations",
        )
    }
    assert await reprocessing.reprocess(session, CALL_ID, kind) == JOB_ID

    read.assert_awaited_once_with(session, "max_attempts")
    awaited = reset.await_args
    assert awaited is not None
    job = awaited.args[1]
    assert awaited.args[0] is session
    assert job.max_attempts == ATTEMPTS[kind.value]
    assert job.lane is JobLane.DAILY
    assert job.lease_seconds == (900 if kind is JobKind.TRANSCRIBE else 300)
    if kind is JobKind.EVALUATE:
        for table, rows in before.items():
            assert connection.execute(text(f"SELECT * FROM {table}")).mappings().all() == rows
        return
    call = connection.execute(text("SELECT * FROM speech_calls")).mappings().one()
    assert (call["processing_status"], call["completed_at"], call["analysis_status"]) == (
        "queued",
        None,
        "pending",
    )
    assert all(
        call[key] is None
        for key in (
            "sentiment",
            "summary",
            "product",
            "ai_node_id",
            "ai_suggestion",
            "ai_suggestion_node_id",
            "call_reason",
            "call_reason_code",
        )
    )
    # Разметка прежним справочником тем — история до SP-310: переобработка её не стирает.
    assert (call["topic_id"], call["ai_topic_label"]) == ("topic", "old ai topic")
    assert (call["segment"], call["segment_source"], call["product_source"]) == (
        "b2b",
        "project",
        "unknown",
    )
    transcript = connection.execute(text("SELECT * FROM speech_transcripts")).mappings().first()
    segment = (
        connection.execute(text("SELECT * FROM speech_transcript_segments")).mappings().first()
    )
    if kind in (JobKind.INGEST, JobKind.TRANSCRIBE):
        assert transcript is segment is None
    else:
        assert segment["text_raw"] == "raw preserved"
        was_llm = method is RoleMethod.LLM_ATTRIBUTION
        assert transcript["role_method"] == ("none" if was_llm else method.value)
        assert segment["speaker"] == ("unknown" if was_llm else "client")
        # Оценка по скрипту стояла на ролях: уходит вместе с догадками модели и
        # остаётся, когда роли измерены. Расшифровка от смены ролей не
        # переписывается, и без удаления оценка пережила бы роли.
        evaluations = connection.execute(
            text("SELECT count(*) FROM speech_call_evaluations")
        ).scalar_one()
        assert evaluations == (0 if was_llm else 1)
    # The service must leave the transaction open, including cascade deletes.
    connection.rollback()
    assert connection.execute(text("SELECT summary FROM speech_calls")).scalar_one() == (
        "old summary"
    )
    assert (
        connection.execute(text("SELECT count(*) FROM speech_transcript_segments")).scalar_one()
        == 1
    )


async def test_the_bulk_reanalysis_replays_in_the_backfill_lane(result_db, monkeypatch):
    reset = AsyncMock(return_value=JOB_ID)
    monkeypatch.setattr(PostgresJobQueue, "reprocess", reset)
    monkeypatch.setattr(reprocessing, "read_setting", AsyncMock(return_value=ATTEMPTS))

    await reprocessing.reprocess(result_db, CALL_ID, JobKind.ANALYZE, lane=JobLane.BACKFILL)

    awaited = reset.await_args
    assert awaited is not None
    assert awaited.args[1].lane is JobLane.BACKFILL


@pytest.mark.parametrize("action", ["retry", "reprocess"])
async def test_route_rolls_back_when_audit_fails(monkeypatch, action):
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(admin_routes, "record", AsyncMock(side_effect=RuntimeError("audit failed")))
    monkeypatch.setattr(admin_routes, "retry", AsyncMock(return_value=(CALL_ID, JobKind.ANALYZE)))
    monkeypatch.setattr(admin_routes, "reprocess", AsyncMock(return_value=JOB_ID))
    with pytest.raises(RuntimeError, match="audit failed"):
        if action == "retry":
            await AdminService(
                PostgresAdminRepository(session, Settings(_env_file=None))
            ).retry_job(RetryJobQuery(job_id=JOB_ID, actor=ADMIN))
        else:
            await AdminService(
                PostgresAdminRepository(session, Settings(_env_file=None))
            ).reprocess_call(
                ReprocessCallQuery(call_id=CALL_ID, actor=ADMIN, from_kind=JobKind.ANALYZE)
            )
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


@pytest.fixture()
async def pg() -> AsyncIterator:
    url = os.getenv("SPEECH_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("SPEECH_POSTGRES_TEST_URL is required for PostgreSQL replay proof")
    schema = "speech_replay_test_" + uuid.uuid4().hex
    owner = create_async_engine(url)
    engine = None
    created = False
    try:
        async with owner.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        # Exclude public: a missing test table must never fall through to real data.
        #
        # Потолка ожидания здесь намеренно нет, хотя раньше был: пока `reprocess`
        # не ставил его сам, стенд подставлял свой — и тесты про конкуренцию
        # завершались благодаря стенду, а продакшен в той же ситуации висел.
        # Ограничение обязано приходить из проверяемого кода, иначе проверяется
        # стенд.
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as connection:
            await connection.run_sync(SpeechBase.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        yield sessions, PostgresJobQueue(sessions, engine=engine)
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with owner.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


def _replay_job(call_id: uuid.UUID, kind: JobKind) -> NewJob:
    """То же задание, что и `_new_job`, но для звонка, созданного на стенде."""
    return NewJob(
        call_id=call_id,
        kind=kind,
        lane=JobLane.DAILY,
        max_attempts=ATTEMPTS[kind.value],
        lease_seconds=900 if kind is JobKind.TRANSCRIBE else 300,
    )


async def _completed_pipeline(sessions, *, method=RoleMethod.LLM_ATTRIBUTION):
    now = datetime.now(UTC)
    call_id, transcript_id, topic_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ids = {kind: uuid.uuid4() for kind in JobKind}
    async with sessions() as session, session.begin():
        session.add(SpeechTopic(id=topic_id, code="test-topic", label="Test"))
        await session.flush()
        session.add(
            SpeechCall(
                id=call_id,
                session_id=uuid.uuid4().hex,
                started_at=now,
                ended_at=now,
                duration_s=120,
                audio_status="stored",
                audio_channels=1,
                processing_status="done",
                completed_at=now,
                analysis_status="done",
                topic_id=topic_id,
                summary="old summary",
                ai_topic_label="old ai topic",
                ai_suggestion=["old", "suggestion"],
                sentiment="negative",
                segment="b2b",
                segment_source="project",
                product="mobile",
                product_source="llm",
            )
        )
        session.add(SpeechSetting(key="max_attempts", value=ATTEMPTS))
        await session.flush()
        session.add(
            SpeechTranscript(
                id=transcript_id,
                call_id=call_id,
                engine="test",
                model_name="fixture",
                role_method=method,
                segments_count=1,
            )
        )
        await session.flush()
        session.add(
            SpeechTranscriptSegment(
                transcript_id=transcript_id,
                call_id=call_id,
                seq=0,
                speaker="client",
                start_ms=0,
                end_ms=1000,
                text_raw="raw text",
            )
        )
        for kind, job_id in ids.items():
            session.add(
                SpeechJob(
                    id=job_id,
                    call_id=call_id,
                    kind=kind,
                    lane="backfill",
                    priority=100,
                    status="done",
                    attempts=3,
                    max_attempts=3,
                    lease_seconds=300,
                    started_at=now,
                    finished_at=now,
                    last_error_class="timeout",
                    last_error_at=now,
                )
            )
    return call_id, ids


async def _snapshot(sessions):
    async with sessions() as session:
        return {
            table: [
                dict(row)
                for row in (
                    await session.execute(text(f"SELECT * FROM {table} ORDER BY id"))
                ).mappings()
            ]
            for table in (
                "speech_calls",
                "speech_jobs",
                "speech_transcripts",
                "speech_transcript_segments",
                "speech_audit_events",
            )
        }


@pytest.mark.parametrize("kind", list(JobKind))
async def test_postgres_completed_pipeline_queues_the_requested_stage(pg, kind):
    sessions, queue = pg
    call_id, ids = await _completed_pipeline(sessions)
    async with sessions() as session:
        response = await AdminService(
            PostgresAdminRepository(session, Settings(_env_file=None))
        ).reprocess_call(ReprocessCallQuery(call_id=call_id, actor=ADMIN, from_kind=kind))
    assert response.changed == 1
    state = await _snapshot(sessions)
    rows = {row["kind"]: row for row in state["speech_jobs"]}
    stage_index = list(JobKind).index(kind)
    assert set(rows) == {k.value for k in list(JobKind)[: stage_index + 1]}
    selected = rows[kind.value]
    assert selected["id"] == ids[kind]
    assert selected["status"] == "queued" and selected["attempts"] == 0
    assert (selected["lane"], selected["priority"]) == ("daily", 0)
    assert selected["max_attempts"] == ATTEMPTS[kind.value]
    assert selected["lease_seconds"] == (900 if kind is JobKind.TRANSCRIBE else 300)
    assert all(
        selected[key] is None
        for key in (
            "locked_by",
            "locked_at",
            "started_at",
            "finished_at",
            "last_error_class",
            "last_error_at",
        )
    )
    assert (state["speech_calls"][0]["completed_at"] is None) == (kind is not JobKind.EVALUATE)
    assert len(state["speech_audit_events"]) == 1


@pytest.mark.parametrize("active_kind", list(JobKind))
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_postgres_cross_stage_active_conflict_is_atomic(pg, active_kind, status):
    sessions, _queue = pg
    call_id, ids = await _completed_pipeline(sessions)
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET status = :status WHERE id = :id"),
            {"id": ids[active_kind], "status": status},
        )
    before = await _snapshot(sessions)
    async with sessions() as session:
        with pytest.raises(ApplicationError) as error:
            await AdminService(
                PostgresAdminRepository(session, Settings(_env_file=None))
            ).reprocess_call(
                ReprocessCallQuery(call_id=call_id, actor=ADMIN, from_kind=JobKind.ANALYZE)
            )
        assert error.value.kind is ErrorKind.CONFLICT
    assert await _snapshot(sessions) == before


@pytest.mark.parametrize("action", ["retry", "reprocess"])
async def test_postgres_audit_failure_rolls_back_jobs_call_outputs_and_audit(
    pg, monkeypatch, action
):
    sessions, _queue = pg
    call_id, ids = await _completed_pipeline(sessions)
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET status = 'dead' WHERE id = :id"),
            {"id": ids[JobKind.ANALYZE]},
        )
    before = await _snapshot(sessions)
    original_record = admin_routes.record

    async def fail_after_insert(*args, **kwargs):
        await original_record(*args, **kwargs)
        raise RuntimeError("audit failed")

    monkeypatch.setattr(admin_routes, "record", fail_after_insert)
    async with sessions() as session:
        with pytest.raises(RuntimeError, match="audit failed"):
            if action == "retry":
                await AdminService(
                    PostgresAdminRepository(session, Settings(_env_file=None))
                ).retry_job(RetryJobQuery(job_id=ids[JobKind.ANALYZE], actor=ADMIN))
            else:
                await AdminService(
                    PostgresAdminRepository(session, Settings(_env_file=None))
                ).reprocess_call(
                    ReprocessCallQuery(call_id=call_id, actor=ADMIN, from_kind=JobKind.ANALYZE)
                )
    assert await _snapshot(sessions) == before


async def test_postgres_concurrent_replays_serialize_on_call(pg):
    sessions, _queue = pg
    call_id, _ids = await _completed_pipeline(sessions)

    async def attempt(kind):
        async with sessions() as session:
            try:
                await AdminService(
                    PostgresAdminRepository(session, Settings(_env_file=None))
                ).reprocess_call(ReprocessCallQuery(call_id=call_id, actor=ADMIN, from_kind=kind))
                return 200
            except ApplicationError as exc:
                return {ErrorKind.CONFLICT: 409, ErrorKind.NOT_FOUND: 404}[exc.kind]

    outcomes = await asyncio.wait_for(
        asyncio.gather(attempt(JobKind.TRANSCRIBE), attempt(JobKind.ANALYZE)), timeout=10
    )
    assert sorted(outcomes) == [200, 409]
    state = await _snapshot(sessions)
    assert sum(row["status"] == "queued" for row in state["speech_jobs"]) == 1
    assert len(state["speech_audit_events"]) == 1


async def test_postgres_locked_job_refuses_replay_without_waiting(pg):
    sessions, _queue = pg
    call_id, ids = await _completed_pipeline(sessions)
    before = await _snapshot(sessions)
    async with sessions() as worker_session, worker_session.begin():
        await worker_session.execute(
            text("SELECT id FROM speech_jobs WHERE id = :id FOR UPDATE"),
            {"id": ids[JobKind.TRANSCRIBE]},
        )
        async with sessions() as admin_session:
            with pytest.raises(ApplicationError) as error:
                await AdminService(
                    PostgresAdminRepository(admin_session, Settings(_env_file=None))
                ).reprocess_call(
                    ReprocessCallQuery(call_id=call_id, actor=ADMIN, from_kind=JobKind.ANALYZE)
                )
            assert error.value.kind is ErrorKind.CONFLICT
    assert await _snapshot(sessions) == before


async def test_postgres_a_held_call_row_gives_up_instead_of_hanging(pg):
    """`FOR UPDATE` на звонке ограничен по времени, и ограничение своё.

    Соседний тест держит строку **задания**, где стоит `NOWAIT`, — там ожидания
    нет вовсе. Здесь держится строка **звонка**, где ожидание разрешено, и
    проверяется, что оно конечно.

    Сессии явно объявляется «жди сколько угодно». Единственное, что после этого
    может вернуть запрос, — потолок, поставленный самим `reprocess`. Без него
    тест не падает с ошибкой, а зависает, и его снимает `wait_for` — ровно то,
    что видел администратор в интерфейсе.
    """
    sessions, queue = pg
    call_id, _ids = await _completed_pipeline(sessions)
    before = await _snapshot(sessions)

    async def replay():
        async with sessions() as admin_session, admin_session.begin():
            await admin_session.execute(text("SET LOCAL lock_timeout = 0"))
            await queue.reprocess(admin_session, _replay_job(call_id, JobKind.ANALYZE))

    async with sessions() as worker_session, worker_session.begin():
        await worker_session.execute(
            text("SELECT id FROM speech_calls WHERE id = :id FOR UPDATE"), {"id": call_id}
        )
        started = asyncio.get_running_loop().time()
        with pytest.raises(ReprocessingConflict, match="pipeline_is_busy"):
            await asyncio.wait_for(replay(), timeout=postgres.CALL_LOCK_TIMEOUT_MS / 1000 + 10)
        waited = asyncio.get_running_loop().time() - started

    # Ожидание было (иначе это `NOWAIT`, а не потолок) и оно кончилось.
    assert waited >= postgres.CALL_LOCK_TIMEOUT_MS / 1000 * 0.5
    assert waited < postgres.CALL_LOCK_TIMEOUT_MS / 1000 + 5
    assert await _snapshot(sessions) == before


async def test_postgres_a_brief_overlap_on_the_call_is_waited_out(pg):
    """Ради этого потолок, а не `NOWAIT`.

    Воркер держит строку звонка коротко: долгая часть этапа идёт вне
    транзакции. Отказывать администратору из-за перекрытия в доли секунды не за
    что — `NOWAIT` отказывал бы, и «переобработать» срывалось бы тем чаще, чем
    загруженнее конвейер.
    """
    sessions, queue = pg
    call_id, _ids = await _completed_pipeline(sessions)

    async def hold_briefly():
        async with sessions() as worker_session, worker_session.begin():
            await worker_session.execute(
                text("SELECT id FROM speech_calls WHERE id = :id FOR UPDATE"), {"id": call_id}
            )
            await asyncio.sleep(0.5)

    holding = asyncio.create_task(hold_briefly())
    await asyncio.sleep(0.1)
    async with sessions() as admin_session, admin_session.begin():
        job_id = await queue.reprocess(admin_session, _replay_job(call_id, JobKind.ANALYZE))
    await holding

    assert job_id is not None


async def test_postgres_the_route_gives_up_too_not_only_the_queue(pg):
    """Потолок обязан действовать на том пути, которым ходит интерфейс.

    `SET LOCAL` вне транзакции — не ошибка, а молчаливое ничто: Postgres лишь
    предупреждает в журнале. Маршрут своей транзакции не открывает, её начинает
    сессия сама на первом запросе, — и если бы этого не происходило, потолок не
    действовал бы ровно там, где нужен, а проверка на очереди всё равно была бы
    зелёной.
    """
    sessions, _queue = pg
    call_id, _ids = await _completed_pipeline(sessions)
    before = await _snapshot(sessions)

    async with sessions() as worker_session, worker_session.begin():
        await worker_session.execute(
            text("SELECT id FROM speech_calls WHERE id = :id FOR UPDATE"), {"id": call_id}
        )
        started = asyncio.get_running_loop().time()
        async with sessions() as admin_session:
            with pytest.raises(ApplicationError) as refused:
                await asyncio.wait_for(
                    AdminService(
                        PostgresAdminRepository(admin_session, Settings(_env_file=None))
                    ).reprocess_call(
                        ReprocessCallQuery(call_id=call_id, actor=ADMIN, from_kind=JobKind.ANALYZE)
                    ),
                    timeout=postgres.CALL_LOCK_TIMEOUT_MS / 1000 + 10,
                )
        waited = asyncio.get_running_loop().time() - started

    assert refused.value.kind is ErrorKind.CONFLICT
    assert waited < postgres.CALL_LOCK_TIMEOUT_MS / 1000 + 5
    assert await _snapshot(sessions) == before


@pytest.mark.parametrize("method", list(RoleMethod))
async def test_postgres_reanalysis_keeps_the_text_and_its_index_and_role_provenance(pg, method):
    sessions, _queue = pg
    call_id, _ids = await _completed_pipeline(sessions, method=method)
    async with sessions() as session:
        await AdminService(
            PostgresAdminRepository(session, Settings(_env_file=None))
        ).reprocess_call(
            ReprocessCallQuery(call_id=call_id, actor=ADMIN, from_kind=JobKind.ANALYZE)
        )
    state = await _snapshot(sessions)
    call = state["speech_calls"][0]
    transcript = state["speech_transcripts"][0]
    segment = state["speech_transcript_segments"][0]
    assert call["completed_at"] is None
    assert call["analysis_status"] == "pending"
    assert call["summary"] is call["sentiment"] is None
    assert call["topic_id"] is not None and call["ai_topic_label"] == "old ai topic"
    assert call["ai_node_id"] is call["ai_suggestion"] is call["ai_suggestion_node_id"] is None
    assert (call["segment"], call["segment_source"]) == ("b2b", "project")
    assert (call["product"], call["product_source"]) == (None, "unknown")
    was_llm = method is RoleMethod.LLM_ATTRIBUTION
    assert transcript["role_method"] == ("none" if was_llm else method.value)
    assert segment["speaker"] == ("unknown" if was_llm else "client")
    # Текст и индекс поиска повторный разбор не трогает: звонок находится и
    # пока ждёт новой разметки.
    assert segment["text_raw"] == "raw text"
    assert segment["search_vector"]


@pytest.mark.parametrize("kind", list(JobKind))
@pytest.mark.parametrize("status", ["failed", "dead"])
async def test_postgres_retry_repairs_call_and_preserves_upstream_jobs(pg, kind, status):
    sessions, _queue = pg
    call_id, ids = await _completed_pipeline(sessions)
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET status = :status WHERE id = :id"),
            {"id": ids[kind], "status": status},
        )
        await session.execute(
            text(
                "UPDATE speech_calls SET processing_status = 'failed', analysis_status = 'failed' "
                "WHERE id = :id"
            ),
            {"id": call_id},
        )
    before = await _snapshot(sessions)
    async with sessions() as session:
        response = await AdminService(
            PostgresAdminRepository(session, Settings(_env_file=None))
        ).retry_job(RetryJobQuery(job_id=ids[kind], actor=ADMIN))
    assert response.changed == 1
    state = await _snapshot(sessions)
    rows = {row["kind"]: row for row in state["speech_jobs"]}
    prior = list(JobKind)[: list(JobKind).index(kind)]
    assert set(rows) == {stage.value for stage in [*prior, kind]}
    for row in before["speech_jobs"]:
        if row["kind"] in prior:
            assert rows[row["kind"]] == row
    selected = rows[kind.value]
    assert selected["id"] == ids[kind]
    assert selected["status"] == "queued" and selected["attempts"] == 0
    assert selected["max_attempts"] == ATTEMPTS[kind.value]
    call = state["speech_calls"][0]
    if kind is JobKind.EVALUATE:
        assert call == before["speech_calls"][0]
    else:
        assert call["processing_status"] == "queued"
        assert call["completed_at"] is None and call["analysis_status"] == "pending"
    assert [row["event_type"] for row in state["speech_audit_events"]] == ["job.retry"]


@pytest.mark.parametrize("status", ["queued", "running"])
async def test_postgres_retry_does_not_overwrite_an_active_pipeline(pg, status):
    sessions, _queue = pg
    _call_id, ids = await _completed_pipeline(sessions)
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET status = 'dead' WHERE id = :id"),
            {"id": ids[JobKind.TRANSCRIBE]},
        )
        await session.execute(
            text("UPDATE speech_jobs SET status = :status WHERE id = :id"),
            {"id": ids[JobKind.ANALYZE], "status": status},
        )
    before = await _snapshot(sessions)
    async with sessions() as session:
        with pytest.raises(ApplicationError) as error:
            await AdminService(
                PostgresAdminRepository(session, Settings(_env_file=None))
            ).retry_job(RetryJobQuery(job_id=ids[JobKind.TRANSCRIBE], actor=ADMIN))
        assert error.value.kind is ErrorKind.CONFLICT
    assert await _snapshot(sessions) == before


async def test_postgres_reprocess_inserts_a_missing_stage_and_rejects_duplicate(pg):
    sessions, _queue = pg
    call_id, ids = await _completed_pipeline(sessions)
    async with sessions() as session, session.begin():
        await session.execute(
            text("DELETE FROM speech_jobs WHERE id = :id"), {"id": ids[JobKind.ANALYZE]}
        )
    async with sessions() as session:
        await AdminService(
            PostgresAdminRepository(session, Settings(_env_file=None))
        ).reprocess_call(
            ReprocessCallQuery(call_id=call_id, actor=ADMIN, from_kind=JobKind.ANALYZE)
        )
    state = await _snapshot(sessions)
    queued = [row for row in state["speech_jobs"] if row["status"] == "queued"]
    assert len(queued) == 1
    assert queued[0]["id"] != ids[JobKind.ANALYZE]
    assert (queued[0]["kind"], queued[0]["lane"], queued[0]["priority"]) == ("analyze", "daily", 0)
    async with sessions() as session:
        with pytest.raises(ApplicationError) as error:
            await AdminService(
                PostgresAdminRepository(session, Settings(_env_file=None))
            ).reprocess_call(
                ReprocessCallQuery(call_id=call_id, actor=ADMIN, from_kind=JobKind.ANALYZE)
            )
        assert error.value.kind is ErrorKind.CONFLICT
    assert await _snapshot(sessions) == state
