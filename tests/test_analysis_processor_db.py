"""PostgreSQL: прямой LLM, актуальность снимка, nonce, атомарность и восстановление analyze."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.analysis.execution import AnalysisExecutionService
from src.application.analysis.processing import AnalysisProcessingService
from src.application.dto.analysis_processing import analysis_claim
from src.application.dto.calls import JobKind, JobLane, JobStatus
from src.application.ports.queue import NewJob
from src.application.transcription_delivery import TranscriptionDeliveryService
from src.di.analysis import build_analysis_client
from src.infrastructure.persistence.analysis_processing import (
    PostgresAnalysisProcessingRepository,
    PostgresAnalysisProcessingTransaction,
)
from src.infrastructure.persistence.classifier import label_key
from src.infrastructure.persistence.transcription_delivery import (
    PostgresTranscriptionReceptionRepository,
)
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.infrastructure.runtime.analysis_processor import AnalysisProcessor
from src.setting.config import Settings
from tests.speech_db_support import speech_test_database_url
from tests.test_speech_queue_protocol import _make_call
from tests.test_transcription_delivery_db import AUTH, TOKEN, _running

_DATABASE = speech_test_database_url()
pytestmark = pytest.mark.skipif(not _DATABASE, reason="SPEECH_POSTGRES_TEST_URL is required")
PATH = ["Физ.лицо", "ШПД", "Консультация", "Не работает", "Wi-Fi"]
TEXTS = ("PRIVATE_OPERATOR_TRANSCRIPT", "PRIVATE_CLIENT_TRANSCRIPT")


@pytest.fixture
async def env():
    """Создать запись и классификатор в выделенной тестовой базе, затем очистить её."""
    assert _DATABASE is not None
    engine = create_async_engine(_DATABASE)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def clean():
        async with sessions() as session, session.begin():
            await session.execute(text("DELETE FROM speech_jobs"))
            await session.execute(text("DELETE FROM speech_calls"))
            await session.execute(text("DELETE FROM speech_call_reasons"))
            for level in range(5, 0, -1):
                await session.execute(
                    text("DELETE FROM speech_classifier_nodes WHERE level = :level"),
                    {"level": level},
                )

    await clean()
    queue = PostgresJobQueue(sessions, engine=engine)
    call_id = await _make_call(queue)
    transcript_id = uuid4()
    nodes = [uuid4() for _ in PATH]
    async with sessions() as session, session.begin():
        for index, label in enumerate(PATH):
            await session.execute(
                text(
                    "INSERT INTO speech_classifier_nodes "
                    "(id, parent_id, level, label, key, source, "
                    "sort_order, is_active, first_seen_at, created_at, updated_at) "
                    "VALUES (:id, :parent, :level, :label, :key, 'file', "
                    ":order, true, now(), now(), now())"
                ),
                {
                    "id": nodes[index],
                    "parent": nodes[index - 1] if index else None,
                    "level": index + 1,
                    "order": index + 1,
                    "label": label,
                    "key": label_key(label),
                },
            )
        await session.execute(
            text(
                "UPDATE speech_calls SET audio_status = 'stored', processing_status = 'analyzing', "
                "analysis_status = 'pending', source = 'telphin', segment = 'b2b', "
                "segment_source = 'project' WHERE id = :id"
            ),
            {"id": call_id},
        )
        await session.execute(
            text(
                "INSERT INTO speech_transcripts "
                "(id, call_id, engine, model_name, language, role_method, "
                "segments_count, created_at) "
                "VALUES (:id, :call, 'whisper', 'test-asr', 'ru', 'none', 2, now())"
            ),
            {"id": transcript_id, "call": call_id},
        )
        for seq, value in enumerate(TEXTS):
            await session.execute(
                text(
                    "INSERT INTO speech_transcript_segments (transcript_id, call_id, seq, speaker, "
                    "start_ms, end_ms, text_raw) "
                    "VALUES (:transcript, :call, :seq, 'unknown', :start, :end, :value)"
                ),
                {
                    "transcript": transcript_id,
                    "call": call_id,
                    "seq": seq,
                    "start": seq * 1000,
                    "end": (seq + 1) * 1000,
                    "value": value,
                },
            )
        await session.execute(
            text(
                "INSERT INTO speech_call_evaluations "
                "(id, call_id, script_version, model_name, script_applies, "
                "stages, objections, evaluated_at) "
                "VALUES (:id, :call, 'old', 'old-model', false, '[]', '[]', now())"
            ),
            {"id": uuid4(), "call": call_id},
        )
        job_id = await queue.enqueue(
            session,
            NewJob(
                call_id=call_id,
                kind=JobKind.ANALYZE,
                lane=JobLane.BACKFILL,
                priority=20,
                max_attempts=3,
            ),
        )
    repository = PostgresAnalysisProcessingRepository(sessions)
    yield SimpleNamespace(
        engine=engine,
        sessions=sessions,
        queue=queue,
        call_id=call_id,
        transcript_id=transcript_id,
        nodes=nodes,
        job_id=job_id,
        repository=repository,
    )
    await clean()
    await engine.dispose()


def _answer(**overrides):
    """Синтетический ответ с распознанными ролями и существующим путём дерева."""
    return {
        "path": PATH,
        "sentiment": "negative",
        "summary": "Новое резюме.",
        "roles": [{"seq": 0, "speaker": "operator"}, {"seq": 1, "speaker": "client"}],
        **overrides,
    }


def _client(monkeypatch, handler):
    """Сохранить настоящий preparer и LLM-клиент; подставить только HTTP-транспорт."""
    original = httpx.AsyncClient
    sent = []

    async def route(request):
        sent.append(request)
        assert request.url.host == "model.test" and request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer llm-secret"
        assert json.loads(request.content)["model"] == "test-model"
        return await handler(request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(route)),
    )
    return build_analysis_client(
        Settings(
            _env_file=None,
            speech_analysis_llm_base_url="http://model.test/v1",
            speech_analysis_llm_api_key="llm-secret",
            speech_analysis_llm_model="test-model",
            speech_worker_api_token="",
        )
    ), sent


async def _response(request):
    """Вернуть корректный Chat Completions ответ без внешней модели."""
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(_answer())}}]})


def _service(env, model):
    """Собрать application поверх настоящей базы и существующей интерпретации."""
    return AnalysisProcessingService(env.repository, model, AnalysisExecutionService())


async def _claim(env, owner="cpu-test"):
    """Захватить только анализ с настоящим nonce очереди."""
    job = await env.queue.claim([JobLane.BACKFILL], owner, kinds=[JobKind.ANALYZE])
    assert job is not None
    return analysis_claim(job, owner)


async def _state(env):
    """Прочитать бизнес-данные и состояние задания без исходных секретов."""
    async with env.sessions() as session:
        call = (
            await session.execute(
                text(
                    "SELECT processing_status, analysis_status, summary, segment, completed_at "
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
        segments = (
            await session.execute(
                text(
                    "SELECT speaker, text_raw FROM speech_transcript_segments "
                    "WHERE call_id = :id ORDER BY seq"
                ),
                {"id": env.call_id},
            )
        ).all()
        scores = await session.scalar(
            text("SELECT count(*) FROM speech_call_evaluations WHERE call_id = :id"),
            {"id": env.call_id},
        )
    return SimpleNamespace(call=call, job=job, segments=segments, scores=scores)


async def test_processor_calls_llm_locally_and_commits_everything(env, monkeypatch) -> None:
    """Прямой HTTP идёт только модели; результат, роли, оценка и done сохраняются атомарно."""
    model, sent = _client(monkeypatch, _response)
    processor = AnalysisProcessor(
        env.queue, _service(env, model), lanes=[JobLane.BACKFILL], worker_id="cpu-test"
    )
    assert await processor.run_once()
    state = await _state(env)
    assert state.job.status == "done" and state.call.processing_status == "done"
    assert state.call.analysis_status == "done" and state.call.summary == "Новое резюме."
    assert state.call.segment == "b2b" and state.call.completed_at is not None
    assert state.scores == 0
    assert [speaker for speaker, value in state.segments] == ["operator", "client"]
    assert [value for speaker, value in state.segments] == list(TEXTS)
    assert len(sent) == 1
    assert not await processor.run_once()


async def test_received_asr_runs_in_cpu_processor_without_delaying_receipt(
    env, monkeypatch
) -> None:
    """Приём сначала подтверждает commit; CPU завершает поставленный анализ отдельно."""
    async with env.sessions() as session, session.begin():
        await session.execute(text("DELETE FROM speech_calls WHERE id = :id"), {"id": env.call_id})
    queue, command = await _running(env.sessions)
    reception = TranscriptionDeliveryService(
        PostgresTranscriptionReceptionRepository(env.sessions), worker_token=TOKEN
    )
    receipt = await reception.receive_authorized(command, AUTH)
    env.call_id = command.claim.call_id
    async with env.sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, priority FROM speech_jobs WHERE call_id = :id AND kind = 'analyze'"
                ),
                {"id": env.call_id},
            )
        ).one()
        env.job_id = row.id
        assert row.priority == 20
    assert (await _state(env)).job.status == "queued"
    entered = asyncio.Event()

    async def respond(request):
        assert env.engine.pool.checkedout() == 0
        entered.set()
        return await _response(request)

    model, sent = _client(monkeypatch, respond)
    processor = AnalysisProcessor(
        queue, _service(env, model), lanes=[JobLane.BACKFILL], worker_id="cpu-test"
    )
    assert await processor.run_once()
    assert entered.is_set() and len(sent) == 1
    assert (await _state(env)).job.status == "done"
    assert await reception.receive_authorized(command, AUTH) == receipt
    assert not await processor.run_once()


@pytest.mark.parametrize("source", ["classifier", "segments"])
async def test_busy_source_is_deferred_without_partial_result(env, monkeypatch, source) -> None:
    """Занятый справочник/текст вызывает отсрочку без расхода попытки и частичной записи."""
    model, _ = _client(monkeypatch, _response)
    claim = await _claim(env)
    async with env.sessions() as session:
        await session.begin()
        try:
            if source == "classifier":
                statement = "UPDATE speech_classifier_nodes SET label = label WHERE id = :id"
                key = env.nodes[-1]
            else:
                statement = (
                    "SELECT seq FROM speech_transcript_segments WHERE call_id = :id FOR UPDATE"
                )
                key = env.call_id
            await session.execute(text(statement), {"id": key})
            result = await _service(env, model).process(claim)
            assert result.status == "deferred"
        finally:
            await session.rollback()
    state = await _state(env)
    assert state.job.status == "queued" and state.job.attempts == 0
    assert state.call.summary is None and state.scores == 1


async def test_late_dead_projection_does_not_overwrite_new_audio_stage(env, monkeypatch) -> None:
    """Старый lease_expired не меняет звонок, уже поставленный на новую расшифровку."""
    await _claim(env)
    async with env.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_jobs SET attempts = max_attempts, "
                "locked_at = now() - interval '1 hour' WHERE id = :id"
            ),
            {"id": env.job_id},
        )
    expired = await env.queue.requeue_expired(kinds=[JobKind.ANALYZE])
    assert len(expired) == 1 and expired[0].status is JobStatus.DEAD
    async with env.sessions() as session, session.begin():
        await env.queue.enqueue(
            session,
            NewJob(
                call_id=env.call_id, kind=JobKind.TRANSCRIBE, lane=JobLane.DAILY, max_attempts=3
            ),
        )
        await session.execute(
            text("UPDATE speech_calls SET processing_status = 'queued' WHERE id = :id"),
            {"id": env.call_id},
        )
    model, _ = _client(monkeypatch, _response)
    service = _service(env, model)
    await service.mark_expired(expired[0])
    await service.repair_expired_failures([JobLane.BACKFILL])
    state = await _state(env)
    assert state.call.processing_status == "queued" and state.call.analysis_status == "pending"


@pytest.mark.parametrize("change", ["text", "roles", "transcript", "classifier", "reason"])
async def test_changed_model_input_rejects_even_valid_output(env, monkeypatch, change) -> None:
    """При правке входа отклонить ответ LLM, сохранить оценку и вернуть попытку."""

    async def respond(request):
        async with env.sessions() as session, session.begin():
            if change in {"text", "roles"}:
                column, value = (
                    ("text_raw", "CHANGED_INPUT") if change == "text" else ("speaker", "operator")
                )
                await session.execute(
                    text(
                        f"UPDATE speech_transcript_segments SET {column} = :value "
                        "WHERE call_id = :id AND seq = 0"
                    ),
                    {"id": env.call_id, "value": value},
                )
            elif change == "transcript":
                await session.execute(
                    text(
                        "UPDATE speech_transcripts "
                        "SET created_at = created_at + interval '1 second' WHERE call_id = :id"
                    ),
                    {"id": env.call_id},
                )
            elif change == "classifier":
                await session.execute(
                    text("UPDATE speech_classifier_nodes SET label = 'Новая тема' WHERE id = :id"),
                    {"id": env.nodes[-1]},
                )
            else:
                await session.execute(
                    text(
                        "INSERT INTO speech_call_reasons "
                        "(code, label, hint, sort_order, is_active, created_at, updated_at) "
                        "VALUES ('K01', 'Новая причина', '', 0, true, now(), now())"
                    )
                )
        return await _response(request)

    model, _ = _client(monkeypatch, respond)
    assert (await _service(env, model).process(await _claim(env))).status == "deferred"
    state = await _state(env)
    assert state.job.status == "queued" and state.job.attempts == 0
    assert state.call.summary is None and state.call.analysis_status == "pending"
    assert state.scores == 1


async def test_stale_nonce_cannot_call_model_or_touch_new_attempt(env, monkeypatch) -> None:
    """Одинаковые имя владельца и номер попытки не дают права старому захвату."""
    model, sent = _client(monkeypatch, _response)
    old = await _claim(env)
    assert await env.repository.defer(old)
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET run_after = now() WHERE id = :id"), {"id": env.job_id}
        )
    current = await _claim(env)
    assert current.attempt == old.attempt and current.claim_token != old.claim_token
    assert (await _service(env, model).process(old)).status == "stale"
    assert not await env.repository.defer(old)
    assert not await env.queue.heartbeat(old.job_id, old.worker_id, claim_token=old.claim_token)
    assert await env.queue.heartbeat(
        current.job_id, current.worker_id, claim_token=current.claim_token
    )
    assert not sent
    assert (await _state(env)).job.status == "running"


async def test_last_lease_check_rolls_back_analysis_and_role_changes(env, monkeypatch) -> None:
    """Истечение аренды на последнем UPDATE откатывает даже уже записанные роли и снятие оценки."""
    model, _ = _client(monkeypatch, _response)
    original = PostgresAnalysisProcessingTransaction.complete

    async def expire(transaction, claim):
        await transaction._session.execute(
            text("UPDATE speech_jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": claim.job_id},
        )
        return await original(transaction, claim)

    monkeypatch.setattr(PostgresAnalysisProcessingTransaction, "complete", expire)
    assert (await _service(env, model).process(await _claim(env))).status == "stale"
    state = await _state(env)
    assert state.call.summary is None and state.call.analysis_status == "pending"
    assert state.scores == 1 and all(speaker == "unknown" for speaker, value in state.segments)
    assert state.job.status == "running"


async def test_timeout_keeps_transcript_and_uses_queue_backoff(env, monkeypatch, caplog) -> None:
    """Сетевой отказ возвращает analyze, сохраняя исходный текст и текущую оценку."""

    async def timeout(request):
        raise httpx.ReadTimeout("PRIVATE_CLIENT_TRANSCRIPT and llm-secret", request=request)

    model, sent = _client(monkeypatch, timeout)
    result = await _service(env, model).process(await _claim(env))
    state = await _state(env)
    assert result.status == "retry" and state.job.last_error_class == "timeout"
    assert state.job.status == "queued" and state.job.attempts == 1
    assert [value for speaker, value in state.segments] == list(TEXTS)
    assert state.scores == 1 and len(sent) == 1
    assert "llm-secret" not in caplog.text and "PRIVATE_CLIENT_TRANSCRIPT" not in caplog.text


async def test_three_invalid_outputs_fail_analysis_but_keep_searchable_text(
    env, monkeypatch
) -> None:
    """Прежний предел формата три; терминальный отказ оставляет звонок и расшифровку доступными."""

    async def invalid(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    model, sent = _client(monkeypatch, invalid)
    assert (await _service(env, model).process(await _claim(env))).status == "dead"
    state = await _state(env)
    assert len(sent) == 3 and state.job.status == "dead"
    assert state.call.processing_status == "done" and state.call.analysis_status == "failed"
    assert [value for speaker, value in state.segments] == list(TEXTS)


async def test_known_stereo_roles_and_existing_grade_are_preserved(env, monkeypatch) -> None:
    """LLM не переписывает измеренные роли; оценка по прежнему входу остаётся."""
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_transcripts SET role_method = 'stereo_energy' WHERE call_id = :id"),
            {"id": env.call_id},
        )
        await session.execute(
            text(
                "UPDATE speech_transcript_segments "
                "SET speaker = CASE seq WHEN 0 THEN 'operator' ELSE 'client' END "
                "WHERE call_id = :id"
            ),
            {"id": env.call_id},
        )
    model, _ = _client(monkeypatch, _response)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    assert (await _state(env)).scores == 1


async def test_recovery_only_touches_analysis_and_repairs_interrupted_projection(
    env, monkeypatch
) -> None:
    """Сборщик ограничен analyze; незаписанный отказ звонка восстанавливается позже."""
    claim = await _claim(env)
    async with env.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_jobs "
                "SET attempts = max_attempts, locked_at = now() - interval '1 hour' WHERE id = :id"
            ),
            {"id": env.job_id},
        )
        other = await _make_call(env.queue)
        audio_id = await env.queue.enqueue(
            session,
            NewJob(call_id=other, kind=JobKind.TRANSCRIBE, lane=JobLane.DAILY, max_attempts=1),
        )
    audio = await env.queue.claim([JobLane.DAILY], "audio-worker", kinds=[JobKind.TRANSCRIBE])
    assert audio is not None
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": audio_id},
        )
    expired = await env.queue.requeue_expired(kinds=[JobKind.ANALYZE], lanes=[JobLane.BACKFILL])
    assert (
        len(expired) == 1 and expired[0].id == claim.job_id and expired[0].status is JobStatus.DEAD
    )
    assert (await _state(env)).call.analysis_status == "pending"
    model, _ = _client(monkeypatch, _response)
    await _service(env, model).repair_expired_failures([JobLane.BACKFILL])
    assert (await _state(env)).call.analysis_status == "failed"
    async with env.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT status FROM speech_jobs WHERE id = :id"), {"id": audio_id}
            )
            == "running"
        )


async def test_cancel_before_commit_is_reclaimed_with_new_nonce(env) -> None:
    """Отмена оставляет running до reaper; перезапуск получает новый захват, текст сохраняется."""
    claim = await _claim(env)
    entered = asyncio.Event()

    async def blocked(call_id, data, model):
        entered.set()
        await asyncio.Event().wait()

    service = AnalysisProcessingService(
        env.repository, SimpleNamespace(), SimpleNamespace(execute=blocked)
    )
    task = asyncio.create_task(service.process(claim))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await _state(env)).job.status == "running"
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": env.job_id},
        )
    await env.queue.requeue_expired(kinds=[JobKind.ANALYZE])
    recovered = await _claim(env, "cpu-restarted")
    assert recovered.claim_token != claim.claim_token
    assert [value for speaker, value in (await _state(env)).segments] == list(TEXTS)
