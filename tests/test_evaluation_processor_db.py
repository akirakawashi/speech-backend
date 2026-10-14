"""PostgreSQL: локальная многошаговая оценка, актуальность, nonce и атомарность."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from src.application.analysis.execution import AnalysisExecutionService
from src.application.analysis.processing import AnalysisProcessingService
from src.application.dto.calls import JobKind, JobLane
from src.application.dto.job_processing import job_processing_claim
from src.application.dto.quality import CURRENT_SCRIPT
from src.application.ports.queue import NewJob
from src.application.quality.processing import EvaluationProcessingService
from src.application.quality.processor_schedule import EvaluationProcessorSchedule
from src.application.quality.scheduling import WorkerMaintenanceService
from src.application.transcription_delivery import TranscriptionDeliveryService
from src.di.analysis import build_analysis_client
from src.domain.calls.schedule import CollectionSchedule
from src.infrastructure.clients.quality import build_quality_client
from src.infrastructure.persistence.analysis_processing import PostgresAnalysisProcessingRepository
from src.infrastructure.persistence.classifier import label_key
from src.infrastructure.persistence.evaluation import PostgresEvaluationRepository
from src.infrastructure.persistence.evaluation_processing import (
    PostgresEvaluationProcessingRepository,
    PostgresEvaluationProcessingTransaction,
)
from src.infrastructure.persistence.quality_jobs import PostgresQualityQueueRepository
from src.infrastructure.persistence.transcription_delivery import (
    PostgresTranscriptionReceptionRepository,
)
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.infrastructure.runtime.job_processor import JobProcessor
from src.setting.config import Settings
from tests.speech_db_support import speech_test_database_url
from tests.test_speech_quality_db import _answer, _call, sessions as sessions
from tests.test_transcription_delivery_db import AUTH, TOKEN, _running

pytestmark = pytest.mark.skipif(
    not speech_test_database_url(), reason="SPEECH_POSTGRES_TEST_URL is required"
)


@pytest.fixture
async def env(sessions):
    """Создать готовый Телфин и независимое evaluate только в защищённой тестовой базе."""
    call_id = await _call(sessions, "cpu-evaluation")
    queue = PostgresJobQueue(sessions, engine=sessions.kw["bind"])
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_calls SET completed_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": call_id},
        )
        job_id = await queue.enqueue(
            session,
            NewJob(
                call_id=call_id,
                kind=JobKind.EVALUATE,
                lane=JobLane.BACKFILL,
                priority=30,
                max_attempts=3,
                payload={"script_version": CURRENT_SCRIPT.version},
            ),
        )
        await session.execute(
            text("UPDATE speech_jobs SET run_after = now() - interval '1 day' WHERE id = :id"),
            {"id": job_id},
        )
        await session.execute(
            text(
                "UPDATE speech_transcripts SET created_at = now() - interval '1 day' "
                "WHERE call_id = :id"
            ),
            {"id": call_id},
        )
    yield SimpleNamespace(
        sessions=sessions,
        engine=sessions.kw["bind"],
        queue=queue,
        call_id=call_id,
        job_id=job_id,
        repository=PostgresEvaluationProcessingRepository(sessions),
    )


async def _state(env):
    """Прочитать задание, звонок и оценки для проверки отсутствия частичной записи."""
    async with env.sessions() as session:
        call = (
            await session.execute(
                text(
                    "SELECT processing_status, analysis_status, completed_at "
                    "FROM speech_calls WHERE id = :id"
                ),
                {"id": env.call_id},
            )
        ).one()
        job = (
            await session.execute(
                text(
                    "SELECT status, attempts, last_error_class, payload "
                    "FROM speech_jobs WHERE id = :id"
                ),
                {"id": env.job_id},
            )
        ).one()
        scores = (
            await session.execute(
                text(
                    "SELECT model_name, script_version, score, failure_reason, script_applies "
                    "FROM speech_call_evaluations WHERE call_id = :id ORDER BY script_version"
                ),
                {"id": env.call_id},
            )
        ).all()
        lines = (
            await session.execute(
                text(
                    "SELECT seq, speaker, text_raw FROM speech_transcript_segments "
                    "WHERE call_id = :id ORDER BY seq"
                ),
                {"id": env.call_id},
            )
        ).all()
        method = await session.scalar(
            text("SELECT role_method FROM speech_transcripts WHERE call_id = :id"),
            {"id": env.call_id},
        )
    return SimpleNamespace(call=call, job=job, scores=scores, lines=lines, method=method)


async def _claim(env, owner="cpu-evaluation"):
    """Получить настоящий nonce evaluate из общей очереди."""
    job = await env.queue.claim([JobLane.BACKFILL], owner, kinds=[JobKind.EVALUATE])
    assert job is not None
    return job_processing_claim(job, owner)


def _client(env, monkeypatch, handler):
    """Сохранить настоящий клиент/промпты; заменить только HTTP модели."""
    original = httpx.AsyncClient
    sent = []

    async def route(request):
        """Проверить прямой маршрут/параметры и отсутствие удерживаемого соединения базы."""
        assert request.url.host == "quality.test" and request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer PRIVATE_QUALITY_KEY"
        body = json.loads(request.content)
        assert body["model"] == "cpu-quality-model" and body["temperature"] == 0
        assert body["max_tokens"] == 4096 and "chat_template_kwargs" not in body
        assert env.engine.pool.checkedout() == 0
        sent.append(request)
        return await handler(request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(route)),
    )
    return build_quality_client(
        Settings(
            _env_file=None,
            speech_quality_llm_base_url="http://quality.test/v1",
            speech_quality_llm_api_key="PRIVATE_QUALITY_KEY",
            speech_quality_llm_model="cpu-quality-model",
            speech_worker_api_token="",
        )
    ), sent


async def _response(request):
    """Вернуть синтетическую оценку и такой же ответ на её проверку."""
    return httpx.Response(200, json={"choices": [{"message": {"content": _answer()}}]})


def _service(env, model):
    """Собрать application поверх настоящего PostgreSQL-порта."""
    return EvaluationProcessingService(env.repository, model)


async def _mono(env):
    """Сделать вход моно-записью без доверенных ролей для проверки атрибуции."""
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_transcripts SET role_method = 'none' WHERE call_id = :id"),
            {"id": env.call_id},
        )
        await session.execute(
            text("UPDATE speech_transcript_segments SET speaker = 'unknown' WHERE call_id = :id"),
            {"id": env.call_id},
        )


async def _grade(env, *, script="old", later=False):
    """Добавить отдельную успешную оценку для проверки сохранения или атомарного снятия."""
    async with env.sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_call_evaluations "
                "(id, call_id, script_version, model_name, script_applies, "
                "score, passed_count, stage_count, "
                "all_passed, stages, objections, evaluated_at) "
                "VALUES (:id, :call, :script, 'manual-model', true, 57, 4, 7, false, '[]', '[]', "
                "clock_timestamp() + make_interval(secs => :later))"
            ),
            {"id": uuid4(), "call": env.call_id, "script": script, "later": 1 if later else 0},
        )


async def test_evaluation_is_local_multistep_and_committed_with_job(env, monkeypatch) -> None:
    """Два LLM-вызова и один commit; готовность, время завершения и доверенные роли сохраняются."""
    before = await _state(env)
    model, sent = _client(env, monkeypatch, _response)
    processor = JobProcessor(
        env.queue,
        {JobKind.EVALUATE: _service(env, model)},
        lanes=[JobLane.BACKFILL],
        worker_id="cpu-evaluation",
    )
    assert await processor.run_once()
    after = await _state(env)
    assert after.job.status == "done" and after.call == before.call
    assert len(sent) == 2 and len(after.scores) == 1
    score = after.scores[0]
    assert score.model_name == "cpu-quality-model" and score.failure_reason is None
    assert score.script_version == CURRENT_SCRIPT.version and score.script_applies
    assert after.lines == before.lines and after.method == "stereo_energy"
    assert not await processor.run_once()


@pytest.mark.parametrize("outcome", ["success", "model_error", "undetermined"])
async def test_mono_roles_and_quality_outcome_are_committed_together(
    env, monkeypatch, outcome
) -> None:
    """Моно-атрибуция сохраняется и при позднем отказе; неоднозначные роли не подменяются."""
    await _mono(env)
    await _grade(env)
    before = await _state(env)
    if outcome == "model_error":
        async with env.sessions() as session, session.begin():
            await session.execute(
                text("UPDATE speech_jobs SET max_attempts = 1 WHERE id = :id"), {"id": env.job_id}
            )
    count = 0

    async def respond(request):
        """Сначала вернуть атрибуцию, затем оценку или нормализуемый сетевой отказ."""
        nonlocal count
        count += 1
        if count == 1 or outcome == "undetermined":
            speakers = (
                ("unknown", "unknown") if outcome == "undetermined" else ("operator", "client")
            )
            content = json.dumps(
                {
                    "roles": [
                        {"seq": index, "speaker": speaker} for index, speaker in enumerate(speakers)
                    ]
                }
            )
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        if outcome == "model_error":
            raise httpx.ReadTimeout("timeout", request=request)
        return await _response(request)

    model, _ = _client(env, monkeypatch, respond)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    after = await _state(env)
    assert after.call == before.call and after.job.status == "done"
    current = next(
        score for score in after.scores if score.script_version == CURRENT_SCRIPT.version
    )
    if outcome == "undetermined":
        assert after.method == "none" and all(line.speaker == "unknown" for line in after.lines)
        assert current.failure_reason == "roles_undetermined" and current.score is None
    else:
        assert after.method == "llm_attribution" and [line.speaker for line in after.lines] == [
            "operator",
            "client",
        ]
        assert len(after.scores) == 1
        assert current.failure_reason == ("model_error" if outcome == "model_error" else None)


@pytest.mark.parametrize(
    "change", ["text", "speaker", "timing", "created_at", "identity", "method", "source"]
)
async def test_changed_source_defers_and_preserves_existing_grade(env, monkeypatch, change) -> None:
    """Даже одинаковый текст нового транскрипта не разрешает сохранение старой оценки."""
    await _grade(env)
    changed = False

    async def respond(request):
        """Изменить вход только во время первого запроса модели, вне транзакции application."""
        nonlocal changed
        if not changed:
            async with env.sessions() as session, session.begin():
                if change in {"text", "speaker", "timing"}:
                    column, value = {
                        "text": ("text_raw", "CHANGED"),
                        "speaker": ("speaker", "client"),
                        "timing": ("start_ms", 800),
                    }[change]
                    await session.execute(
                        text(
                            f"UPDATE speech_transcript_segments SET {column} = :value "
                            "WHERE call_id = :id AND seq = 0"
                        ),
                        {"id": env.call_id, "value": value},
                    )
                elif change == "identity":
                    old = (
                        await session.execute(
                            text("SELECT created_at FROM speech_transcripts WHERE call_id = :id"),
                            {"id": env.call_id},
                        )
                    ).scalar_one()
                    lines = (
                        (
                            await session.execute(
                                text(
                                    "SELECT seq, speaker, start_ms, end_ms, text_raw "
                                    "FROM speech_transcript_segments "
                                    "WHERE call_id = :id ORDER BY seq"
                                ),
                                {"id": env.call_id},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    await session.execute(
                        text("DELETE FROM speech_transcripts WHERE call_id = :id"),
                        {"id": env.call_id},
                    )
                    transcript = uuid4()
                    await session.execute(
                        text(
                            "INSERT INTO speech_transcripts "
                            "(id, call_id, engine, model_name, language, role_method, "
                            "segments_count, created_at) "
                            "VALUES (:id, :call, 'test', 'same', 'ru', "
                            "'stereo_energy', 2, :created)"
                        ),
                        {"id": transcript, "call": env.call_id, "created": old},
                    )
                    for line in lines:
                        await session.execute(
                            text(
                                "INSERT INTO speech_transcript_segments "
                                "(transcript_id, call_id, seq, speaker, "
                                "start_ms, end_ms, text_raw) "
                                "VALUES (:t, :c, :seq, :speaker, :start_ms, :end_ms, :text_raw)"
                            ),
                            {**line, "t": transcript, "c": env.call_id},
                        )
                elif change == "created_at":
                    await session.execute(
                        text(
                            "UPDATE speech_transcripts "
                            "SET created_at = created_at + interval '1 second' WHERE call_id = :id"
                        ),
                        {"id": env.call_id},
                    )
                elif change == "method":
                    await session.execute(
                        text(
                            "UPDATE speech_transcripts SET role_method = 'llm_attribution' "
                            "WHERE call_id = :id"
                        ),
                        {"id": env.call_id},
                    )
                else:
                    await session.execute(
                        text("UPDATE speech_calls SET source = 'ncc' WHERE id = :id"),
                        {"id": env.call_id},
                    )
            changed = True
        return await _response(request)

    model, _ = _client(env, monkeypatch, respond)
    assert (await _service(env, model).process(await _claim(env))).status == "deferred"
    state = await _state(env)
    assert state.job.status == "queued" and state.job.attempts == 0
    assert len(state.scores) == 1 and state.scores[0].model_name == "manual-model"


async def test_last_nonce_check_rolls_back_grade_roles_and_old_grade_removal(
    env, monkeypatch
) -> None:
    """Истечение аренды на финальном UPDATE откатывает всю оценку и атрибуцию."""
    await _mono(env)
    await _grade(env)
    count = 0

    async def respond(request):
        """Сначала определить роли, затем дать пригодную оценку и проверку."""
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "roles": [
                                            {"seq": 0, "speaker": "operator"},
                                            {"seq": 1, "speaker": "client"},
                                        ],
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        return await _response(request)

    original = PostgresEvaluationProcessingTransaction.complete

    async def expire(transaction, claim):
        """Просрочить аренду внутри UoW перед последним условным UPDATE."""
        await transaction._session.execute(
            text("UPDATE speech_jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": claim.job_id},
        )
        return await original(transaction, claim)

    monkeypatch.setattr(PostgresEvaluationProcessingTransaction, "complete", expire)
    model, _ = _client(env, monkeypatch, respond)
    assert (await _service(env, model).process(await _claim(env))).status == "stale"
    state = await _state(env)
    assert state.job.status == "running" and state.method == "none"
    assert all(line.speaker == "unknown" for line in state.lines)
    assert len(state.scores) == 1 and state.scores[0].model_name == "manual-model"


async def test_old_nonce_cannot_compute_or_touch_same_owner_and_attempt(env, monkeypatch) -> None:
    """Имя процесса и возвращённый номер попытки не дают старому nonce права вызова/записи."""
    model, sent = _client(env, monkeypatch, _response)
    old = await _claim(env)
    assert await env.repository.defer(old)
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET run_after = now() WHERE id = :id"), {"id": env.job_id}
        )
    current = await _claim(env)
    assert current.attempt == old.attempt and current.claim_token != old.claim_token
    assert (await _service(env, model).process(old)).status == "stale"
    assert not sent and not await env.repository.defer(old)
    assert not await env.queue.heartbeat(old.job_id, old.worker_id, claim_token=old.claim_token)
    assert (await _state(env)).job.status == "running"


@pytest.mark.parametrize("last", [False, True])
async def test_timeout_uses_budget_and_preserves_call(env, monkeypatch, caplog, last) -> None:
    """До последней попытки сеть повторяется; на последней отказ и done сохраняются вместе."""
    if last:
        async with env.sessions() as session, session.begin():
            await session.execute(
                text("UPDATE speech_jobs SET max_attempts = 1 WHERE id = :id"), {"id": env.job_id}
            )
    before = await _state(env)

    async def timeout(request):
        """Выдать ошибку с чувствительным текстом, который клиент должен нормализовать."""
        raise httpx.ReadTimeout("PRIVATE_QUALITY_KEY PRIVATE_OPERATOR_TEXT", request=request)

    model, _ = _client(env, monkeypatch, timeout)
    outcome = await _service(env, model).process(await _claim(env))
    state = await _state(env)
    assert state.call == before.call and state.lines == before.lines
    assert outcome.status == ("done" if last else "retry")
    if last:
        assert state.job.status == "done" and state.scores[0].failure_reason == "model_error"
    else:
        assert (
            state.job.status == "queued"
            and state.job.last_error_class == "timeout"
            and not state.scores
        )
    assert "PRIVATE_QUALITY_KEY" not in caplog.text and "PRIVATE_OPERATOR_TEXT" not in caplog.text


async def test_invalid_output_is_failure_without_zero_score(env, monkeypatch) -> None:
    """Три неприменимых JSON дают invalid_evaluation с NULL баллом и сохранённым готовым звонком."""
    before = await _state(env)

    async def invalid(request):
        """Вернуть JSON без обязательных полей оценки."""
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    model, sent = _client(env, monkeypatch, invalid)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    state = await _state(env)
    assert len(sent) == 3 and state.job.status == "done" and state.call == before.call
    assert state.scores[0].failure_reason == "invalid_evaluation" and state.scores[0].score is None


@pytest.mark.parametrize("later", [False, True])
async def test_existing_success_survives_failure_or_late_older_success(
    env, monkeypatch, later
) -> None:
    """Сетевой отказ не снимает прежний успех; запоздавший успех не заменяет более свежий."""
    if not later:
        await _grade(env, script=CURRENT_SCRIPT.version)
        async with env.sessions() as session, session.begin():
            await session.execute(
                text("UPDATE speech_jobs SET max_attempts = 1 WHERE id = :id"), {"id": env.job_id}
            )
    added = False

    async def respond(request):
        """Поставить более свежую оценку во время LLM либо выдать терминальный сетевой отказ."""
        nonlocal added
        if not later:
            raise httpx.ReadTimeout("timeout", request=request)
        if not added:
            await _grade(env, script=CURRENT_SCRIPT.version, later=True)
            added = True
        return await _response(request)

    model, _ = _client(env, monkeypatch, respond)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    state = await _state(env)
    assert len(state.scores) == 1 and state.scores[0].model_name == "manual-model"
    assert state.scores[0].score == 57 and state.scores[0].failure_reason is None


@pytest.mark.parametrize("source", ["ncc", "missing", "pending", "upstream"])
async def test_ineligible_input_never_reaches_model(env, monkeypatch, source) -> None:
    """Оценка не вызывается для NCC/отсутствующего текста и ждёт предшествующих этапов."""
    async with env.sessions() as session, session.begin():
        if source == "ncc":
            await session.execute(
                text("UPDATE speech_calls SET source = 'ncc' WHERE id = :id"), {"id": env.call_id}
            )
        elif source == "missing":
            await session.execute(
                text("DELETE FROM speech_transcripts WHERE call_id = :id"), {"id": env.call_id}
            )
        elif source == "pending":
            await session.execute(
                text("UPDATE speech_calls SET processing_status = 'queued' WHERE id = :id"),
                {"id": env.call_id},
            )
        else:
            await env.queue.enqueue(
                session,
                NewJob(
                    call_id=env.call_id, kind=JobKind.ANALYZE, lane=JobLane.DAILY, max_attempts=3
                ),
            )
    before = await _state(env)
    model, sent = _client(env, monkeypatch, _response)
    status = (await _service(env, model).process(await _claim(env))).status
    after = await _state(env)
    assert status == ("deferred" if source in {"pending", "upstream"} else "dead")
    assert not sent and not after.scores and after.call == before.call


async def test_busy_segment_defers_without_partial_grade(env, monkeypatch) -> None:
    """Занятые реплики не вызывают встречного ожидания или частичной записи."""
    model, _ = _client(env, monkeypatch, _response)
    claim = await _claim(env)
    async with env.sessions() as session:
        await session.begin()
        try:
            await session.execute(
                text("SELECT seq FROM speech_transcript_segments WHERE call_id = :id FOR UPDATE"),
                {"id": env.call_id},
            )
            assert (await _service(env, model).process(claim)).status == "deferred"
        finally:
            await session.rollback()
    state = await _state(env)
    assert state.job.status == "queued" and state.job.attempts == 0 and not state.scores


async def test_cancel_is_recovered_with_new_nonce_without_changing_call(env, monkeypatch) -> None:
    """Отмена оставляет running; свой сборщик сохраняет чужие этапы и готовность звонка."""
    entered = asyncio.Event()

    async def blocked(request):
        """Удерживать только HTTP, оставляя базу свободной."""
        entered.set()
        await asyncio.Event().wait()

    before = await _state(env)
    model, _ = _client(env, monkeypatch, blocked)
    claim = await _claim(env)
    task = asyncio.create_task(_service(env, model).process(claim))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": env.job_id},
        )
    await env.queue.requeue_expired(kinds=[JobKind.EVALUATE], lanes=[JobLane.BACKFILL])
    current = await _claim(env, "restarted-cpu")
    assert current.claim_token != claim.claim_token
    state = await _state(env)
    assert state.call == before.call and not state.scores


async def test_asr_analysis_and_scheduled_evaluation_share_one_cpu_process(
    env, monkeypatch
) -> None:
    """После квитанции один CPU-процесс выполняет анализ, затем независимо выбранную оценку."""
    from datetime import UTC, datetime, timedelta

    path = ["Физ.лицо", "ШПД", "Консультация", "Не работает", "Wi-Fi"]
    nodes = [uuid4() for _ in path]
    async with env.sessions() as session, session.begin():
        await session.execute(text("DELETE FROM speech_calls WHERE id = :id"), {"id": env.call_id})
        for index, label in enumerate(path):
            await session.execute(
                text(
                    "INSERT INTO speech_classifier_nodes "
                    "(id, parent_id, level, label, key, source, sort_order, is_active, "
                    "first_seen_at, created_at, updated_at) "
                    "VALUES (:id, :parent, :level, :label, :key, 'file', "
                    ":order, true, now(), now(), now())"
                ),
                {
                    "id": nodes[index],
                    "parent": nodes[index - 1] if index else None,
                    "level": index + 1,
                    "label": label,
                    "key": label_key(label),
                    "order": index + 1,
                },
            )
    try:
        queue, command = await _running(env.sessions)
        env.call_id = command.claim.call_id
        reception = TranscriptionDeliveryService(
            PostgresTranscriptionReceptionRepository(env.sessions), worker_token=TOKEN
        )
        receipt = await reception.receive_authorized(command, AUTH)
        original = httpx.AsyncClient
        sent = []

        async def route(request):
            """Подставить только модели и убедиться в освобождении базы на каждом вызове."""
            assert env.engine.pool.checkedout() == 0
            body = json.loads(request.content)
            sent.append(body["model"])
            if body["model"] == "cpu-analysis-model":
                answer = json.dumps({"path": path, "sentiment": "positive", "summary": "Резюме."})
                return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})
            assert body["model"] == "cpu-quality-model"
            return await _response(request)

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(route)),
        )
        settings = Settings(
            _env_file=None,
            speech_analysis_llm_base_url="http://quality.test/v1",
            speech_analysis_llm_model="cpu-analysis-model",
            speech_quality_llm_base_url="http://quality.test/v1",
            speech_quality_llm_model="cpu-quality-model",
            speech_worker_api_token="",
        )
        analysis = AnalysisProcessingService(
            PostgresAnalysisProcessingRepository(env.sessions),
            build_analysis_client(settings),
            AnalysisExecutionService(),
        )
        evaluation = _service(env, build_quality_client(settings))
        policy = CollectionSchedule()
        scheduler = EvaluationProcessorSchedule(
            WorkerMaintenanceService(
                policy,
                PostgresQualityQueueRepository(env.sessions),
                PostgresEvaluationRepository(env.sessions),
            ),
            policy,
        )
        processor = JobProcessor(
            queue,
            {JobKind.ANALYZE: analysis, JobKind.EVALUATE: evaluation},
            lanes=[JobLane.BACKFILL],
            recoveries={JobKind.ANALYZE: analysis},
            schedule=scheduler,
            worker_id="one-cpu",
        )
        assert await processor.run_once()
        assert sent == ["cpu-analysis-model"]
        assert not await processor.run_once()
        await scheduler.tick(now=datetime.now(UTC) + timedelta(minutes=16))
        assert await processor.run_once()
        assert sent == ["cpu-analysis-model", "cpu-quality-model", "cpu-quality-model"]
        async with env.sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT kind, status, priority FROM speech_jobs "
                        "WHERE call_id = :id ORDER BY kind"
                    ),
                    {"id": env.call_id},
                )
            ).all()
            assert [(row.kind, row.status) for row in rows] == [
                ("analyze", "done"),
                ("evaluate", "done"),
                ("transcribe", "done"),
            ]
            assert next(row.priority for row in rows if row.kind == "evaluate") == 30
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM speech_call_evaluations "
                        "WHERE call_id = :id AND script_version = :script"
                    ),
                    {"id": env.call_id, "script": CURRENT_SCRIPT.version},
                )
                == 1
            )
        assert await reception.receive_authorized(command, AUTH) == receipt
    finally:
        async with env.sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM speech_calls WHERE id = :id"), {"id": env.call_id}
            )
            for node in reversed(nodes):
                await session.execute(
                    text("DELETE FROM speech_classifier_nodes WHERE id = :id"), {"id": node}
                )
