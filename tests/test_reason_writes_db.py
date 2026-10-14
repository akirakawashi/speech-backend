"""PostgreSQL: частичная причина, независимое происхождение и защита от позднего ответа."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from src.application.dto.analysis_storage import AnalysisBindingKind, LegacyAnalysisProvenance
from src.application.dto.analysis_writes import SaveReason
from src.application.reasons.batch import compute_reason, fill_reasons
from src.domain.calls.reasons import prompt_reasons
from src.infrastructure.persistence.analysis_results import AnalysisResultStore
from src.infrastructure.persistence.reasons import PostgresReasonRepository
from tests.test_analysis_processor_db import (
    _answer,
    _claim,
    _client,
    _service,
    _state,
    env as env,
    pytestmark as pytestmark,
)
from tests.test_analysis_result_writes_db import _reason_reference, _stored
from tests.test_speech_fill_reasons import _call

ANSWER = {"call_reason": "Нет связи", "call_reason_code": "K05"}


async def _command(env, client):
    """Загрузить согласованный снимок, закрыть чтение и вызвать реальный HTTP-клиент."""
    source = await PostgresReasonRepository(env.sessions).load(env.call_id)
    assert source is not None
    result = await compute_reason(
        client.model_copy(update={"scenario": "reason"}),
        env.call_id,
        prompt_reasons(source.reasons),
        frozenset(reason.code for reason in source.reasons),
        source.transcript.segments,
    )
    assert result is not None
    return SaveReason(source=source, result=result)


def _model(env, monkeypatch, *, full=False):
    """Подставить только HTTP: один полный анализ при необходимости, затем причины."""
    answers = [_answer()] if full else []

    async def respond(request):
        assert env.engine.pool.checkedout() == 0
        answer = answers.pop(0) if answers else ANSWER
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    return _client(monkeypatch, respond)[0]


async def test_reason_before_full_analysis_creates_only_partial_result(env, monkeypatch):
    """Запись причины не выставляет done и не выдумывает классификацию/резюме."""
    await _reason_reference(env)
    model = _model(env, monkeypatch)
    command = await _command(env, model)
    assert await PostgresReasonRepository(env.sessions).save(command)
    result = await _stored(env)
    assert result is not None and result.analysis_provenance is None
    assert result.binding_kind is AnalysisBindingKind.VERIFIED and result.revision == 1
    assert result.call_reason_code == "K05" and result.summary is None
    assert result.reason_provenance.scenario == "reason" and result.reason_provenance.job is None
    assert result.reason_provenance.source_fingerprint == command.source.fingerprint
    state = await _state(env)
    assert state.call.analysis_status == "pending" and state.job.status == "queued"
    assert state.call.summary is None and state.scores == 1
    assert not await PostgresReasonRepository(env.sessions).save(command)
    assert await _stored(env) == result


async def test_reason_does_not_replace_full_analysis_fields_or_provenance(env, monkeypatch):
    """Частичная запись увеличивает ревизию, сохраняя полный анализ от прежнего запроса."""
    await _reason_reference(env)
    model = _model(env, monkeypatch, full=True)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    before = await _stored(env)
    command = await _command(env, model)
    assert await PostgresReasonRepository(env.sessions).save(command)
    after = await _stored(env)
    assert after.id == before.id and after.revision == before.revision + 1
    assert after.analysis_provenance == before.analysis_provenance
    assert after.analysis_reference_id == before.analysis_reference_id
    assert (after.ai_node_id, after.ai_path, after.summary, after.llm_segment) == (
        before.ai_node_id,
        before.ai_path,
        before.summary,
        before.llm_segment,
    )
    assert after.reason_provenance.execution_id != before.analysis_provenance.execution_id
    assert after.reason_reference_id != after.analysis_reference_id


async def test_reason_preserves_legacy_projection_without_inventing_metadata(env, monkeypatch):
    """Старые результаты и длинное резюме сохраняются; проект не выдаётся за ответ LLM."""
    await _reason_reference(env)
    summary = "Прежнее резюме " * 40
    async with env.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE speech_calls SET ai_node_id = :node, sentiment = 'negative', "
                "summary = :summary, product = 'mobile', product_source = 'llm' WHERE id = :id"
            ),
            {"id": env.call_id, "node": env.nodes[-1], "summary": summary},
        )
    command = await _command(env, _model(env, monkeypatch))
    assert await PostgresReasonRepository(env.sessions).save(command)
    result = await _stored(env)
    assert result.summary == summary and result.ai_node_id == env.nodes[-1]
    assert result.binding_kind is AnalysisBindingKind.LEGACY_OBSERVED
    assert isinstance(result.analysis_provenance, LegacyAnalysisProvenance)
    assert result.analysis_reference_id is None and result.ai_path is None
    assert result.llm_segment is None and result.llm_product == "mobile"
    assert result.reason_provenance.origin == "llm"
    assert "requested_model" in result.analysis_provenance.unknown_fields
    assert (await _state(env)).call.segment == "b2b"


@pytest.mark.parametrize(
    "change",
    [
        "text",
        "roles",
        "transcript",
        "reason_disabled",
        "reason_changed",
        "revision",
        "identity",
    ],
)
async def test_late_reason_is_rejected_after_input_or_result_change(env, monkeypatch, change):
    """Изменение текста/ролей/справочника и пересоздание строки запрещают прежнюю причину."""
    await _reason_reference(env)
    model = _model(env, monkeypatch, full=True)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    command = await _command(env, model)
    async with env.sessions() as session, session.begin():
        if change in {"text", "roles"}:
            column, value = (
                ("text_raw", "Изменённая реплика") if change == "text" else ("speaker", "unknown")
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
                    "UPDATE speech_transcripts SET created_at = created_at + interval '1 second' "
                    "WHERE call_id = :id"
                ),
                {"id": env.call_id},
            )
        elif change == "reason_disabled":
            await session.execute(
                text("UPDATE speech_call_reasons SET is_active = false WHERE code = 'K05'")
            )
        elif change == "reason_changed":
            await session.execute(
                text("UPDATE speech_call_reasons SET hint = 'Другое правило' WHERE code = 'K05'")
            )
        elif change == "revision":
            await session.execute(
                text("UPDATE speech_call_analyses SET revision = revision + 1 WHERE call_id = :id"),
                {"id": env.call_id},
            )
        else:
            await session.execute(
                text("UPDATE speech_call_analyses SET id = gen_random_uuid() WHERE call_id = :id"),
                {"id": env.call_id},
            )
    before = await _stored(env)
    assert not await PostgresReasonRepository(env.sessions).save(command)
    assert await _stored(env) == before
    async with env.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT call_reason_code FROM speech_calls WHERE id = :id"),
                {"id": env.call_id},
            )
            is None
        )


async def test_competing_reasons_publish_exactly_one_result(env, monkeypatch):
    """Два одинаковых исходных снимка не перезаписывают ответ победившего процесса."""
    await _reason_reference(env)
    model = _model(env, monkeypatch)
    first, second = await _command(env, model), await _command(env, model)
    repository = PostgresReasonRepository(env.sessions)
    saved = await asyncio.gather(repository.save(first), repository.save(second))
    assert sorted(saved) == [False, True]
    result = await _stored(env)
    winner = first if saved[0] else second
    assert result.revision == 1
    assert result.reason_provenance.execution_id == winner.result.execution.execution_id


async def test_busy_transcript_leaves_reason_unmodified(env, monkeypatch):
    """Занятые реплики дают False без частичного снимка/результата."""
    await _reason_reference(env)
    command = await _command(env, _model(env, monkeypatch))
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("SELECT seq FROM speech_transcript_segments WHERE call_id = :id FOR UPDATE"),
            {"id": env.call_id},
        )
        assert not await PostgresReasonRepository(env.sessions).save(command)
    assert await _stored(env) is None


async def test_full_analysis_replaces_a_partial_reason_without_archiving_it(env, monkeypatch):
    """Пришедший позже полный анализ заменяет весь набор, включая отсутствие причины."""
    await _reason_reference(env)
    answers = [ANSWER, _answer()]

    async def respond(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(answers.pop(0))}}]}
        )

    model, _ = _client(monkeypatch, respond)
    assert await PostgresReasonRepository(env.sessions).save(await _command(env, model))
    partial = await _stored(env)
    assert (await _service(env, model).process(await _claim(env))).status == "done"
    full = await _stored(env)
    assert full.id == partial.id and full.revision == 2
    assert full.analysis_provenance is not None and full.reason_provenance is None
    assert full.call_reason_code is None


async def test_two_calls_reuse_one_immutable_reason_reference(env, monkeypatch):
    """Конкурентные записи разных звонков сходятся к одному снимку без перезаписи."""
    await _reason_reference(env)
    async with env.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE speech_call_reasons SET hint = :hint WHERE code = 'K05'"),
            {"hint": str(uuid4())},
        )
    other = SimpleNamespace(**vars(env))
    other.call_id = await _call(env.sessions, "other", hours=1)
    model = _model(env, monkeypatch)
    first, second = await _command(env, model), await _command(other, model)
    repo = PostgresReasonRepository(env.sessions)
    assert all(await asyncio.gather(repo.save(first), repo.save(second)))
    saved, another = await _stored(env), await _stored(other)
    assert saved.reason_reference_id == another.reason_reference_id


async def test_batch_counts_rejected_write_as_skipped(env, monkeypatch, capsys):
    """Отчёт CLI не считает сохранённой причину, отвергнутую после смены текста."""
    await _reason_reference(env)

    async def respond(request):
        async with env.sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE speech_transcript_segments SET text_raw = 'Уже другой текст' "
                    "WHERE call_id = :id AND seq = 0"
                ),
                {"id": env.call_id},
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ANSWER)}}]})

    model, _ = _client(monkeypatch, respond)
    report = await fill_reasons(
        PostgresReasonRepository(env.sessions),
        model.model_copy(update={"scenario": "reason"}),
        transcribed_since=datetime.now(UTC) - timedelta(days=1),
        apply=True,
        progress_every=1,
    )
    assert (report.candidates, report.filled, report.skipped, report.failed) == (1, 0, 1, 0)
    assert await _stored(env) is None
    assert "пропущено 1" in capsys.readouterr().out


async def test_reason_failure_after_new_write_rolls_back_legacy_projection(env, monkeypatch):
    """Отказ между новой записью и прежним UPDATE не оставляет половину результата."""
    await _reason_reference(env)
    command = await _command(env, _model(env, monkeypatch))
    original = AnalysisResultStore.put

    async def broken(store, result):
        await original(store, result)
        raise RuntimeError("synthetic reason write failure")

    monkeypatch.setattr(AnalysisResultStore, "put", broken)
    with pytest.raises(RuntimeError, match="synthetic reason write failure"):
        await PostgresReasonRepository(env.sessions).save(command)
    assert await _stored(env) is None
    async with env.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT call_reason_code FROM speech_calls WHERE id = :id"),
                {"id": env.call_id},
            )
            is None
        )
