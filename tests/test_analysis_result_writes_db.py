"""PostgreSQL: две проекции анализа, происхождение, атомарность и снятие результата."""

import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, text

from src.application.dto.analysis_storage import LlmAnalysisProvenance
from src.application.dto.calls import JobKind, JobLane
from src.application.ports.queue import NewJob
from src.application.transcription_delivery import TranscriptionDeliveryService
from src.infrastructure.persistence.analysis_processing import PostgresAnalysisProcessingTransaction
from src.infrastructure.persistence.analysis_results import AnalysisResultStore
from src.infrastructure.persistence.reasons import PostgresReasonRepository
from src.infrastructure.persistence.reprocessing import reprocess
from src.infrastructure.persistence.transcription_delivery import (
    PostgresTranscriptionReceptionRepository,
)
from tests.test_analysis_processor_db import (
    PATH,
    TEXTS,
    _answer,
    _claim,
    _client,
    _response,
    _service,
    _state,
    env as env,
    pytestmark as pytestmark,
)
from tests.test_transcription_delivery_db import AUTH, TOKEN, WORKER, _delivery


async def _stored(env):
    """Прочитать текущий результат отдельной сессией после commit."""
    async with env.sessions() as session:
        return await AnalysisResultStore(session).current(env.call_id)


async def _reason_reference(env):
    """Добавить один включённый код до получения модельного снимка."""
    async with env.sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO speech_call_reasons (code, label, hint, sort_order, is_active) "
                "VALUES ('K05', 'Нет связи', '', 0, true)"
            )
        )


async def _again(env):
    """Разрешить вторую попытку полного анализа для проверки замены без replay."""
    async with env.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_jobs SET status = 'queued', attempts = 0, payload = '{}', "
                "run_after = now(), finished_at = NULL WHERE id = :id"
            ),
            {"id": env.job_id},
        )


async def test_success_publishes_facts_references_roles_and_legacy_projection(env, monkeypatch):
    """Один commit сохраняет модельный сегмент отдельно от проектного и настоящий вход."""
    model, sent = _client(monkeypatch, _response)
    claim = await _claim(env)
    original = await env.repository.load(claim)
    assert (await _service(env, model).process(claim)).status == "done"
    result = await _stored(env)
    assert result is not None and result.revision == 1
    assert result.transcript_id == env.transcript_id
    assert result.ai_path == tuple(PATH) and result.ai_node_id == env.nodes[-1]
    assert result.summary == (await _state(env)).call.summary == "Новое резюме."
    assert result.llm_segment == "individual" and (await _state(env)).call.segment == "b2b"
    provenance = result.analysis_provenance
    assert isinstance(provenance, LlmAnalysisProvenance)
    assert provenance.source_fingerprint == original.fingerprint
    assert provenance.requested_model == "test-model" and provenance.served_model is None
    assert provenance.job is not None and provenance.job.claim_token == claim.claim_token
    assert provenance.model_call_index == 1
    current = await PostgresReasonRepository(env.sessions).load(env.call_id)
    assert current is not None
    assert provenance.committed_transcript_fingerprint == current.transcript.fingerprint()
    assert current.transcript.role_method == "llm_attribution"
    assert len(sent) == 1 and (await _state(env)).job.status == "done"
    encoded = provenance.model_dump_json()
    assert "llm-secret" not in encoded and all(value not in encoded for value in TEXTS)
    async with env.sessions() as session, session.begin():
        await session.execute(text("DELETE FROM speech_jobs WHERE id = :id"), {"id": claim.job_id})
    assert await _stored(env) == result


@pytest.mark.parametrize("reason", [{}, {"call_reason": "Нет связи"}, {"call_reason_code": "K05"}])
async def test_reason_from_full_analysis_shares_its_request_facts(env, monkeypatch, reason):
    """Необязательная причина, включая текст без кода, имеет происхождение полного анализа."""
    await _reason_reference(env)

    async def respond(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(_answer(**reason))}}]}
        )

    model, _ = _client(monkeypatch, respond)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    result = await _stored(env)
    assert result is not None
    if reason:
        assert result.reason_provenance == result.analysis_provenance
        assert result.reason_reference_id == result.analysis_reference_id
    else:
        assert result.reason_provenance is None and result.reason_reference_id is None


async def test_full_reanalysis_replaces_one_row_and_reuses_the_reference(env, monkeypatch):
    """Без архива: ревизия растёт, отсутствующая во втором ответе причина снимается."""
    await _reason_reference(env)
    answers = [
        _answer(call_reason="Нет связи", call_reason_code="K05"),
        _answer(summary="Второй итог"),
    ]

    async def respond(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(answers.pop(0))}}]}
        )

    model, _ = _client(monkeypatch, respond)
    service = _service(env, model)
    assert (await service.process(await _claim(env))).status == "done"
    first = await _stored(env)
    await _again(env)
    assert (await service.process(await _claim(env))).status == "done"
    second = await _stored(env)
    assert first is not None and second is not None
    assert first.id == second.id and second.revision == first.revision + 1
    assert second.created_at == first.created_at and second.summary == "Второй итог"
    assert second.analysis_reference_id == first.analysis_reference_id
    assert second.analysis_provenance != first.analysis_provenance
    assert second.call_reason_code is None and second.reason_provenance is None
    async with env.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM speech_call_analyses WHERE call_id = :id"),
                {"id": env.call_id},
            )
            == 1
        )
        old_code = await session.scalar(
            text("SELECT call_reason_code FROM speech_calls WHERE id = :id"), {"id": env.call_id}
        )
        assert old_code is None


@pytest.mark.parametrize("failure", ["lease", "commit"])
async def test_failed_finalization_rolls_back_new_tables_and_old_results(env, monkeypatch, failure):
    """Запись снимка, результата, проекции и ролей исчезает при отказе lease/commit."""
    model, _ = _client(monkeypatch, _response)
    original = PostgresAnalysisProcessingTransaction.complete
    reference_ids = []
    armed = [False]

    def reject_commit(connection):
        if armed[0]:
            armed[0] = False
            raise RuntimeError("synthetic commit failure")

    async def finish(transaction, claim):
        row = await AnalysisResultStore(transaction._session).current(claim.call_id)
        assert row is not None
        reference_ids.append(row.analysis_reference_id)
        if failure == "lease":
            return False
        completed = await original(transaction, claim)
        armed[0] = True
        return completed

    monkeypatch.setattr(PostgresAnalysisProcessingTransaction, "complete", finish)
    event.listen(env.engine.sync_engine, "commit", reject_commit)
    try:
        claim = await _claim(env)
        if failure == "commit":
            with pytest.raises(RuntimeError, match="synthetic commit failure"):
                await _service(env, model).process(claim)
        else:
            assert (await _service(env, model).process(claim)).status == "stale"
    finally:
        event.remove(env.engine.sync_engine, "commit", reject_commit)
    assert await _stored(env) is None
    state = await _state(env)
    assert state.call.summary is None and state.scores == 1 and state.job.status == "running"
    assert all(speaker == "unknown" for speaker, value in state.segments)
    async with env.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM speech_analysis_references WHERE id = :id"),
                {"id": reference_ids[0]},
            )
            == 0
        )


@pytest.mark.parametrize("kind", list(JobKind))
async def test_replay_removes_current_analysis_except_for_evaluation(env, monkeypatch, kind):
    """Повтор анализа/аудио снимает обе проекции; повтор оценки оставляет анализ."""
    model, _ = _client(monkeypatch, _response)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    before = await _stored(env)
    async with env.sessions() as session, session.begin():
        await reprocess(session, env.call_id, kind)
    after = await _stored(env)
    if kind is JobKind.EVALUATE:
        assert after == before
    else:
        assert after is None
        async with env.sessions() as session:
            row = (
                await session.execute(
                    text("SELECT summary, segment FROM speech_calls WHERE id = :id"),
                    {"id": env.call_id},
                )
            ).one()
        assert row.summary is None and row.segment == "b2b"


async def test_failed_replay_transaction_keeps_current_analysis(env, monkeypatch):
    """Ошибка после постановки повтора откатывает также снятие новой записи анализа."""
    model, _ = _client(monkeypatch, _response)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    before = await _stored(env)
    with pytest.raises(RuntimeError, match="synthetic replay failure"):
        async with env.sessions() as session, session.begin():
            await reprocess(session, env.call_id, JobKind.ANALYZE)
            raise RuntimeError("synthetic replay failure")
    assert await _stored(env) == before
    state = await _state(env)
    assert state.job.status == "done" and state.call.summary == before.summary


async def test_new_asr_invalidates_analysis_but_receipt_retry_preserves_new_result(
    env, monkeypatch
):
    """Новый текст снимает анализ; повтор той же квитанции не снимает более свежий анализ."""
    model, _ = _client(monkeypatch, _response)
    service = _service(env, model)
    assert (await service.process(await _claim(env))).status == "done"
    previous = await _stored(env)
    key = f"calls/{uuid4()}.wav"
    async with env.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_calls SET audio_object_key = :key, audio_channels = 2, "
                "processing_status = 'transcribing' WHERE id = :id"
            ),
            {"id": env.call_id, "key": key},
        )
        await env.queue.enqueue(
            session,
            NewJob(
                call_id=env.call_id,
                kind=JobKind.TRANSCRIBE,
                lane=JobLane.BACKFILL,
                max_attempts=3,
                lease_seconds=900,
            ),
        )
    job = await env.queue.claim([JobLane.BACKFILL], WORKER, kinds=[JobKind.TRANSCRIBE])
    assert job is not None
    command = _delivery(job, key)
    reception = TranscriptionDeliveryService(
        PostgresTranscriptionReceptionRepository(env.sessions), worker_token=TOKEN
    )
    receipt = await reception.receive_authorized(command, AUTH)
    assert await _stored(env) is None
    assert (await service.process(await _claim(env))).status == "done"
    fresh = await _stored(env)
    assert fresh.id != previous.id and fresh.transcript_id == receipt.transcript_id
    await reception.receive_authorized(command, AUTH)
    assert await _stored(env) == fresh
