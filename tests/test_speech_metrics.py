"""`/metrics`: ключ, формат и — главное — совпадение чисел с прямым SQL.

Метрика, которая ошибается тихо, хуже отсутствующей: по ней принимают решение,
что всё в порядке. Поэтому здесь не «эндпойнт отвечает 200», а сверка каждого
числа с отдельно написанным запросом к той же базе.
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
from src.infrastructure.observability.metrics import Sample, as_mapping, collect, render
from src.setting.config import Settings, get_settings
from tests.speech_db_support import speech_test_database_url

_TEST_DATABASE_URL = speech_test_database_url()
KEY = "metrics-key-for-tests"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "app_env": "local", "speech_auth_mode": "open"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Формат
# --------------------------------------------------------------------------- #


def test_a_name_is_announced_once_and_its_samples_stay_together() -> None:
    """Повторный `# TYPE` для одного имени сборщик считает ошибкой разбора и
    отбрасывает **весь** ответ, а не одну строку."""
    body = render(
        [
            Sample(
                name="speech_calls",
                value=1,
                labels={"processing_status": "done", "audio_status": "stored"},
            ),
            Sample(name="speech_audio_bytes", value=5, labels={"audio_status": "stored"}),
            Sample(
                name="speech_calls",
                value=2,
                labels={"processing_status": "failed", "audio_status": "stored"},
            ),
        ]
    )

    assert body.count("# TYPE speech_calls ") == 1
    lines = body.splitlines()
    calls = [i for i, line in enumerate(lines) if line.startswith("speech_calls{")]
    assert calls == sorted(calls) and calls[-1] - calls[0] == len(calls) - 1


def test_whole_numbers_are_printed_whole() -> None:
    """`12.0` в счётчике заданий читается как «что-то усреднили»."""
    assert render([Sample(name="speech_calls", value=12.0)]).splitlines()[-1] == "speech_calls 12"
    assert (
        render([Sample(name="speech_stage_seconds_avg", value=1.5)])
        .splitlines()[-1]
        .endswith("1.500")
    )


def test_a_label_value_with_a_quote_cannot_break_the_line() -> None:
    line = Sample(
        name="speech_errors_recent_jobs", value=1, labels={"error_class": 'we"ird'}
    ).render()

    assert line == 'speech_errors_recent_jobs{error_class="we\\"ird"} 1'


def test_every_metric_has_a_declared_type_and_help() -> None:
    """Ошибка в `# TYPE` делает график молча неверным, а не ломает сбор."""
    from src.infrastructure.observability.metrics import _META

    body = render([Sample(name=name, value=1) for name in _META])

    for name, (kind, _help) in _META.items():
        assert f"# TYPE {name} {kind}" in body
        assert f"# HELP {name} " in body


# --------------------------------------------------------------------------- #
# Ключ
# --------------------------------------------------------------------------- #


@pytest.fixture()
async def client() -> AsyncIterator[httpx.AsyncClient]:
    if not _TEST_DATABASE_URL:
        pytest.skip("SPEECH_POSTGRES_TEST_URL is required for the metrics proof")
    settings = _settings(speech_ms_database_url=_TEST_DATABASE_URL, speech_metrics_key=KEY)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        yield http
    app.dependency_overrides.clear()


async def test_without_a_configured_key_the_endpoint_does_not_exist() -> None:
    """`401` сам по себе — сообщение: «тут есть что-то за ключом»."""
    settings = _settings(speech_metrics_key="")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://speech") as http:
        response = await http.get("/metrics")

    assert response.status_code == 404


async def test_a_wrong_key_is_refused(client) -> None:
    response = await client.get("/metrics", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_no_key_at_all_is_refused(client) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 401


async def test_the_right_key_gets_the_prometheus_body(client) -> None:
    response = await client.get("/metrics", headers={"Authorization": f"Bearer {KEY}"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    # База здесь пуста, и это важный случай: пустой ответ не позволяет
    # отличить «звонков нет» от «сбор сломан».
    assert "speech_calls_total 0" in response.text
    assert "speech_jobs_total 0" in response.text


async def test_metrics_are_not_in_the_public_schema(client) -> None:
    """Схема отдаётся фронту; ручка со своим ключом ему не нужна и не его дело."""
    schema = (await client.get("/openapi.json")).json()

    assert "/metrics" not in schema["paths"]


# --------------------------------------------------------------------------- #
# Числа
# --------------------------------------------------------------------------- #


@pytest.fixture()
async def seeded() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not _TEST_DATABASE_URL:
        pytest.skip("SPEECH_POSTGRES_TEST_URL is required for the metrics proof")
    engine = create_async_engine(_TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await _clean(sessions)
    await _seed(sessions)
    yield sessions
    await _clean(sessions)
    await engine.dispose()


async def _clean(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(text("DELETE FROM speech_jobs"))
        await session.execute(text("DELETE FROM speech_calls"))


async def _seed(sessions: async_sessionmaker[AsyncSession]) -> None:
    # Время берётся здесь, а не константой модуля. Возраст задания метрика
    # считает от текущего времени базы, поэтому «час назад», проставленный при
    # импорте, к моменту запроса превращается в «час и сколько-то»: ровно
    # столько, сколько набор тестов шёл от сбора до этого места. В одиночном
    # прогоне это миллисекунды, в полном — минуты, и тест краснел от размера
    # набора, а не от поведения кода.
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        calls: list[uuid.UUID] = []
        for index, (processing, audio) in enumerate(
            [("done", "stored"), ("done", "stored"), ("failed", "unavailable_at_source")]
        ):
            call_id = uuid.uuid4()
            calls.append(call_id)
            await session.execute(
                text(
                    "INSERT INTO speech_calls (id, session_id, direction, started_at, ended_at, "
                    "duration_s, audio_status, audio_bytes, processing_status, analysis_status, "
                    "source_snapshot, first_seen_at, created_at, updated_at) "
                    "VALUES (:id, :sid, 'inbound', :s, :s, 60, :audio, :bytes, :proc, 'done', "
                    "'{}', :s, :s, :s)"
                ),
                {
                    "id": call_id,
                    "sid": f"node_1_metrics_{index}",
                    "s": now - timedelta(hours=2),
                    "audio": audio,
                    "bytes": 1000 * (index + 1),
                    "proc": processing,
                },
            )

        # Задания: две завершённые расшифровки с известной длительностью, одно
        # ждущее в очереди и одно с отказом.
        for index, (kind, lane, status, started, finished, error) in enumerate(
            [
                (
                    "transcribe",
                    "daily",
                    "done",
                    now - timedelta(minutes=30),
                    now - timedelta(minutes=20),
                    None,
                ),
                (
                    "transcribe",
                    "daily",
                    "done",
                    now - timedelta(minutes=15),
                    now - timedelta(minutes=5),
                    None,
                ),
                ("ingest", "daily", "queued", None, None, None),
                (
                    "analyze",
                    "backfill",
                    "failed",
                    now - timedelta(minutes=9),
                    None,
                    "provider_error",
                ),
            ]
        ):
            await session.execute(
                text(
                    "INSERT INTO speech_jobs (id, call_id, kind, lane, status, priority, "
                    "attempts, max_attempts, run_after, lease_seconds, payload, created_at, "
                    "started_at, finished_at, last_error_class, last_error_at) "
                    "VALUES (gen_random_uuid(), :call, :kind, :lane, :status, 100, 1, 5, :now, "
                    "300, '{}', :created, :started, :finished, :err, :err_at)"
                ),
                {
                    "call": calls[index % len(calls)],
                    "kind": kind,
                    "lane": lane,
                    "status": status,
                    "now": now,
                    "created": now - timedelta(hours=1),
                    "started": started,
                    "finished": finished,
                    "err": error,
                    "err_at": now - timedelta(minutes=9) if error else None,
                },
            )


async def _scalar(sessions: async_sessionmaker[AsyncSession], sql: str) -> Any:
    async with sessions() as session:
        return await session.scalar(text(sql))


async def test_the_numbers_match_a_direct_query(seeded) -> None:
    """Сверка с отдельно написанным SQL, а не с тем же запросом дважды."""
    async with seeded() as session:
        values = as_mapping(await collect(session))

    assert values['speech_queue_jobs{lane="daily",status="done"}'] == 2
    assert values['speech_queue_jobs{lane="daily",status="queued"}'] == 1
    assert values['speech_queue_jobs{lane="backfill",status="failed"}'] == 1

    expected_done = await _scalar(
        seeded, "SELECT count(*) FROM speech_jobs WHERE lane = 'daily' AND status = 'done'"
    )
    assert values['speech_queue_jobs{lane="daily",status="done"}'] == expected_done


async def test_stage_durations_match(seeded) -> None:
    async with seeded() as session:
        values = as_mapping(await collect(session))

    assert values['speech_stage_finished_jobs{kind="transcribe"}'] == 2
    # Обе расшифровки по десять минут.
    assert values['speech_stage_seconds_avg{kind="transcribe"}'] == pytest.approx(600, abs=2)
    assert values['speech_stage_seconds_p95{kind="transcribe"}'] == pytest.approx(600, abs=2)


async def test_the_oldest_pending_job_is_reported_per_lane(seeded) -> None:
    """Единственная метрика, по которой срыв ночной обработки виден до утра."""
    async with seeded() as session:
        values = as_mapping(await collect(session))

    assert values['speech_queue_oldest_seconds{lane="daily"}'] == pytest.approx(3600, abs=5)
    assert 'speech_queue_oldest_seconds{lane="backfill"}' not in values, (
        "упавшее задание не ждёт — оно требует человека, и это другая метрика"
    )


async def test_a_call_held_until_the_night_is_not_an_old_job(seeded) -> None:
    """Приём отложил звонок до ночи: он ждёт по расписанию, а не завис с утра."""

    async def hold(not_before: str, *, run_after: str | None = None) -> dict[str, float]:
        async with seeded() as session, session.begin():
            # У первого звонка заготовки задания приёма нет — оно и откладывается.
            await session.execute(
                text("DELETE FROM speech_jobs WHERE kind = 'ingest' AND lane = 'backfill'")
            )
            await session.execute(
                text(
                    "INSERT INTO speech_jobs (id, call_id, kind, lane, status, priority, "
                    "attempts, max_attempts, run_after, lease_seconds, payload, created_at) "
                    "SELECT gen_random_uuid(), id, 'ingest', 'backfill', 'queued', 10, 0, 5, "
                    f"{run_after or not_before}, 300, "
                    f"jsonb_build_object('not_before', {not_before}), "
                    "now() - interval '5 hours' "
                    "FROM speech_calls ORDER BY session_id LIMIT 1"
                )
            )
        async with seeded() as session:
            return as_mapping(await collect(session))

    waiting = await hold("now() + interval '2 hours'")
    assert 'speech_queue_oldest_seconds{lane="backfill"}' not in waiting
    # Счётчик очереди отложенное видит: оно в очереди, просто не сейчас.
    assert waiting['speech_queue_jobs{lane="backfill",status="queued"}'] == 1

    # Ночь началась десять минут назад: столько задание и ждёт, а не пять часов.
    due = await hold("now() - interval '10 minutes'")
    assert due['speech_queue_oldest_seconds{lane="backfill"}'] == pytest.approx(600, abs=5)

    # Ожидание свободного слота переписывает `run_after`. Возраст от него не
    # зависит — иначе очередь, которой не хватает слотов, выглядела бы пустой.
    busy = await hold("now() - interval '10 minutes'", run_after="now() + interval '30 seconds'")
    assert busy['speech_queue_oldest_seconds{lane="backfill"}'] == pytest.approx(600, abs=5)


async def test_errors_are_counted_by_class_and_stage(seeded) -> None:
    async with seeded() as session:
        values = as_mapping(await collect(session))

    assert values['speech_errors_recent_jobs{error_class="provider_error",kind="analyze"}'] == 1


async def test_call_and_byte_counts_match(seeded) -> None:
    async with seeded() as session:
        values = as_mapping(await collect(session))

    assert values['speech_calls{audio_status="stored",processing_status="done"}'] == 2
    assert values['speech_audio_bytes{audio_status="stored"}'] == 3000
    expected = await _scalar(
        seeded,
        "SELECT coalesce(sum(audio_bytes), 0) FROM speech_calls WHERE audio_status = 'stored'",
    )
    assert values['speech_audio_bytes{audio_status="stored"}'] == expected


async def test_bytes_are_reported_once_per_audio_status(seeded) -> None:
    """Две строки с одним набором меток — ошибка разбора, от которой сборщик
    отбрасывает весь ответ."""
    async with seeded() as session:
        body = render(await collect(session))

    series = [line.split(" ")[0] for line in body.splitlines() if not line.startswith("#")]
    assert len(series) == len(set(series))
