"""PostgreSQL и HTTP: решения ingest привязаны к живому nonce и не выдают секреты в ошибках."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.dto.calls import JobKind, JobLane, ProcessingStatus
from src.application.dto.ingest_control import IngestClaim, IngestPlan, IngestResolution
from src.application.ingestion.admission import IngestControlService
from src.application.ports.queue import NewJob
from src.di.http import create_app
from src.infrastructure.persistence.ingest_control import PostgresIngestControlRepository
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.presentation.http.ingest_control import ingest_control_dep
from src.setting.config import Settings
from tests.speech_db_support import speech_test_database_url

URL = speech_test_database_url()
pytestmark = pytest.mark.skipif(not URL, reason="SPEECH_POSTGRES_TEST_URL is required")
TOKEN = "test-ingest-control-token"
PRIVATE_URL = "https://private.invalid/SECRET_RECORDING"


@pytest.fixture
async def env():
    """Поднять сессии отдельной тестовой базы; HTTP и SQL оставить настоящими."""
    engine = create_async_engine(URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def clean():
        async with sessions() as session, session.begin():
            for table in ("speech_jobs", "speech_calls", "speech_settings"):
                await session.execute(text(f"DELETE FROM {table}"))

    await clean()
    queue = PostgresJobQueue(sessions, engine=engine)
    async with sessions() as session, session.begin():
        call_id = await session.scalar(
            text(
                "INSERT INTO speech_calls (id, session_id, started_at, ended_at, duration_s, "
                "direction, audio_status, processing_status, analysis_status, "
                "first_seen_at, created_at, updated_at, source_snapshot, source_audio_url) "
                "VALUES (gen_random_uuid(), :sid, :now, :now, 60, 'unknown', 'pending', "
                "'queued', 'pending', :now, :now, :now, "
                '\'{"audio_locator":"locator"}\'::jsonb, :url) RETURNING id'
            ),
            {
                "sid": "test-" + uuid4().hex,
                "now": datetime(2026, 10, 14, tzinfo=UTC),
                "url": PRIVATE_URL,
            },
        )
        await queue.enqueue(
            session,
            NewJob(
                call_id=call_id,
                kind=JobKind.INGEST,
                lane=JobLane.BACKFILL,
                max_attempts=6,
                priority=100,
            ),
        )
    job = await queue.claim([JobLane.BACKFILL], "worker-test", kinds=[JobKind.INGEST])
    assert job is not None
    claim = IngestClaim(
        call_id=job.call_id,
        job_id=job.id,
        claim_token=job.claim_token,
        worker_id="worker-test",
        attempt=job.attempts,
    )
    service = IngestControlService(PostgresIngestControlRepository(sessions), worker_token=TOKEN)
    app = create_app(Settings(_env_file=None, speech_auth_mode="open", app_env="local"))
    app.dependency_overrides[ingest_control_dep] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://backend",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield SimpleNamespace(
            engine=engine, sessions=sessions, queue=queue, job=job, claim=claim, client=client
        )
    await clean()
    await engine.dispose()


async def prepare(env, claim=None):
    """Получить команду через настоящий роутер и проверить успешный HTTP-контракт."""
    response = await env.client.post(
        "/internal/v1/ingest/prepare", json=(claim or env.claim).model_dump(mode="json")
    )
    assert response.status_code == 200, response.text
    assert env.engine.pool.checkedout() == 0
    return IngestPlan.model_validate_json(response.content)


async def resolve(env, observation, claim=None):
    """Передать техническое наблюдение через роутер; вернуть HTTP-ответ без подмены application."""
    return await env.client.post(
        "/internal/v1/ingest/resolve",
        json={"claim": (claim or env.claim).model_dump(mode="json"), "observation": observation},
    )


@pytest.mark.parametrize("duration,short", [(0, False), (9, True), (10, False), (60, False)])
async def test_backend_chooses_short_recording_boundary_and_preserves_unknown_duration(
    env, duration, short
):
    """Ноль не короткий, граница включается в обработку; только бэкенд отдаёт terminal-команду."""
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_calls SET duration_s=:seconds WHERE id=:id"),
            {"seconds": duration, "id": env.claim.call_id},
        )
    plan = await prepare(env)
    assert (plan.completion is not None) == short
    if short:
        assert plan.completion.stage.processing_status == ProcessingStatus.SKIPPED_SHORT
        assert plan.completion.stage.next_kind is None
    else:
        assert plan.fetch.ref.audio_url == PRIVATE_URL
        assert plan.fetch.check_stored is False
    async with env.sessions() as session:
        row = (
            await session.execute(
                text("SELECT status,payload FROM speech_jobs WHERE id=:id"),
                {"id": env.claim.job_id},
            )
        ).one()
    assert row.status == "running", "решение не завершает техническое задание само"
    assert PRIVATE_URL not in str(row.payload)
    assert row.payload["_ingest_admission"]["claim_token"] == str(env.claim.claim_token)


async def test_policy_snapshot_is_reused_until_nonce_changes(env):
    """Правка порога не меняет решение посреди попытки; новый nonce читает новую политику."""
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_calls SET duration_s=9 WHERE id=:id"), {"id": env.claim.call_id}
        )
    first = await prepare(env)
    async with env.sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_settings (key,value,updated_at) "
                "VALUES ('short_call_threshold_s','0'::jsonb,now())"
            )
        )
    assert await prepare(env) == first
    await env.queue.defer(env.job.id, env.claim.worker_id, delay_seconds=0)
    new = await env.queue.claim([JobLane.BACKFILL], env.claim.worker_id, kinds=[JobKind.INGEST])
    claim = env.claim.model_copy(update={"claim_token": new.claim_token, "attempt": new.attempts})
    assert claim.claim_token != env.claim.claim_token and claim.attempt == env.claim.attempt
    assert (await prepare(env, claim)).fetch is not None
    old = await env.client.post(
        "/internal/v1/ingest/prepare", json=env.claim.model_dump(mode="json")
    )
    assert old.status_code == 409
    assert "SECRET_RECORDING" not in old.text


@pytest.mark.parametrize("last", [False, True])
async def test_backend_decides_missing_retry_or_terminal_by_database_attempt_budget(env, last):
    """До предела — повтор, на последней попытке — штатный skipped_no_audio, без failed."""
    await prepare(env)
    if last:
        async with env.sessions() as session, session.begin():
            await session.execute(
                text("UPDATE speech_jobs SET max_attempts=attempts WHERE id=:id"),
                {"id": env.claim.job_id},
            )
    response = await resolve(env, {"outcome": "missing"})
    assert response.status_code == 200, response.text
    result = IngestResolution.model_validate_json(response.content)
    assert result.retry == (not last)
    if last:
        assert result.completion is not None
        assert result.completion.work.outcome == "no_audio"
        assert result.completion.stage.processing_status == ProcessingStatus.SKIPPED_NO_AUDIO
        assert result.completion.stage.next_kind is None


async def test_stored_observation_returns_measurements_and_only_transcription_transition(env):
    """Исход хранит измерения, а роль дальнейшего технического этапа выбирает бэкенд."""
    plan = await prepare(env)
    response = await resolve(
        env,
        {
            "outcome": "stored",
            "object_key": plan.fetch.object_key,
            "audio_bytes": 123,
            "channels": 2,
            "duration_s": 61,
        },
    )
    result = IngestResolution.model_validate_json(response.content)
    assert result.completion is not None
    assert result.completion.work.channels == 2 and result.completion.work.duration_s == 61
    assert result.completion.stage.next_kind == JobKind.TRANSCRIBE
    assert result.completion.stage.processing_status == ProcessingStatus.TRANSCRIBING
    assert env.engine.pool.checkedout() == 0


@pytest.mark.parametrize("change", ["nonce", "owner", "attempt", "expiry", "kind"])
async def test_stale_or_foreign_claim_cannot_prepare_or_resolve(env, change):
    """UUID задания без той же живой аренды не даёт доступа к источнику или решению."""
    plan = await prepare(env)
    claim = env.claim
    if change == "nonce":
        claim = claim.model_copy(update={"claim_token": uuid4()})
    elif change == "owner":
        claim = claim.model_copy(update={"worker_id": "other-worker"})
    elif change == "attempt":
        claim = claim.model_copy(update={"attempt": 99})
    else:
        async with env.sessions() as session, session.begin():
            expression = (
                "locked_at=now()-interval '1 day'" if change == "expiry" else "kind='transcribe'"
            )
            await session.execute(
                text(f"UPDATE speech_jobs SET {expression} WHERE id=:id"), {"id": claim.job_id}
            )
    response = await env.client.post(
        "/internal/v1/ingest/prepare", json=claim.model_dump(mode="json")
    )
    assert response.status_code == 409
    response = await resolve(env, {"outcome": "stored", "object_key": plan.fetch.object_key}, claim)
    assert response.status_code == 409
    assert PRIVATE_URL not in response.text


async def test_wrong_key_and_unprepared_results_are_rejected_without_changing_call(env):
    """Нельзя объявить сохранённым чужой ключ или подменить ещё не выданную команду."""
    assert (await resolve(env, {"outcome": "missing"})).status_code == 409
    await prepare(env)
    assert (
        await resolve(env, {"outcome": "stored", "object_key": "PRIVATE_WRONG_KEY"})
    ).status_code == 409
    response = await resolve(env, {"outcome": "missing", "object_key": "PRIVATE_WRONG_KEY"})
    assert response.status_code == 422 and "PRIVATE_WRONG_KEY" not in response.text
    async with env.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT audio_status FROM speech_calls WHERE id=:id"),
                {"id": env.claim.call_id},
            )
            == "pending"
        )
