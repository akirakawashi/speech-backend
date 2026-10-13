"""Сравнение моделей читает PostgreSQL и не меняет исходный разговор или очередь."""

import json

import httpx

from src.application.comparison.service import compare
from src.application.dto.comparison import Sampled
from src.di.analysis import build_analysis_client
from src.di.comparison import build_candidate_client
from src.infrastructure.persistence.comparison import PostgresComparisonRepository
from src.setting.config import Settings
from tests.test_analysis_processor_db import (
    PATH,
    _answer,
    _state,
    env as env,
    pytestmark as pytestmark,
)


async def test_comparison_closes_connections_and_never_stores_result(env, monkeypatch):
    """Обе модели получают настоящий снимок; вычисленные роли не записываются в БД."""
    before = await _state(env)
    original = httpx.AsyncClient
    requests = []

    async def respond(request):
        assert env.engine.pool.checkedout() == 0
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(_answer())}}]}
        )

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *args, **kwargs: original(*args, transport=transport, **kwargs)
    )
    settings = Settings(
        _env_file=None,
        speech_worker_api_token="",
        speech_analysis_llm_base_url="https://model.invalid/v1",
        speech_analysis_llm_model="production-model",
        speech_analysis_llm_candidate_model="candidate-model",
    )
    call = Sampled(
        call_id=env.call_id,
        operator_node_id=env.nodes[-1],
        ai_node_id=env.nodes[-1],
        sentiment=None,
        reason_code=None,
    )
    rows = await compare(
        [call],
        repository=PostgresComparisonRepository(env.sessions),
        candidate=build_candidate_client(settings),
        control=build_analysis_client(settings),
        concurrency=1,
        echo=lambda _line: None,
    )
    assert len(requests) == 2 and requests[0]["messages"] == requests[1]["messages"]
    assert rows[0].candidate is not None and rows[0].candidate.work is not None
    assert rows[0].candidate.work.path == tuple(PATH) and rows[0].candidate.work.roles
    assert rows[0].control is not None and rows[0].control.work == rows[0].candidate.work
    assert await _state(env) == before
