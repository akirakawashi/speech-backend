"""Ручная постановка оценки локальна и дедуплицируется в PostgreSQL."""

import httpx
from sqlalchemy import text

from src.application.dto.quality_batch import RunEvaluation
from src.di import evaluation
from src.setting.config import Settings
from tests.test_evaluation_processor_db import (
    env as env,
    pytestmark as pytestmark,
    sessions as sessions,
)


async def test_manual_queue_command_needs_neither_token_nor_model_and_closes_pool(env, monkeypatch):
    """Повтор команды не создаёт вторую оценку; LLM/HTTP не нужны для постановки."""
    async with env.sessions() as session, session.begin():
        await session.execute(text("DELETE FROM speech_jobs WHERE id=:id"), {"id": env.job_id})
    settings = Settings(_env_file=None, speech_worker_api_token="", speech_quality_llm_base_url="")
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    monkeypatch.setattr(evaluation, "get_speech_ms_engine", lambda _settings: env.engine)
    monkeypatch.setattr(evaluation, "get_speech_ms_sessionmaker", lambda _settings: env.sessions)
    original_dispose = type(env.engine).dispose
    disposed = []

    async def dispose(engine):
        disposed.append(engine)
        await original_dispose(engine)

    monkeypatch.setattr(type(env.engine), "dispose", dispose)

    def forbidden_http(*args, **kwargs):
        raise AssertionError("queue selection must not use HTTP")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_http)
    query = RunEvaluation(call=str(env.call_id), apply=True)
    first = await evaluation.run_queued_evaluation(query)
    second = await evaluation.run_queued_evaluation(query)
    assert first.enqueued == 1 and second.enqueued == 0
    assert disposed == [env.engine, env.engine]
    async with env.sessions() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM speech_jobs "
                    "WHERE call_id=:id AND kind='evaluate' AND status='queued'"
                ),
                {"id": env.call_id},
            )
            == 1
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM speech_call_evaluations WHERE call_id=:id"),
                {"id": env.call_id},
            )
            == 0
        )
