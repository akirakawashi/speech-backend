"""Команды бэкенда вызывают модели с локальными промптами и раздельными ключами."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from src.application.comparison.service import compare
from src.application.dto.comparison import Sampled
from src.application.dto.pipeline_input import (
    AnalysisInput,
    AnalysisSegment,
    CallForAnalysis,
    ClassifierRecord,
)
from src.application.errors.external import AnalysisError
from src.application.reasons.batch import ask_reason
from src.di.analysis import build_analysis_client
from src.di.comparison import build_candidate_client
from src.di.reasons import build_reason_client
from src.domain.calls.classifier import label_key
from src.setting.config import Settings
from tests.test_analysis_interpretation import _answer
from tests.test_model_preparation import _inputs


def _settings() -> Settings:
    """Синтетические модельные настройки без адреса API, базы и служебного токена."""
    return Settings(
        _env_file=None,
        speech_backend_base_url="",
        speech_worker_api_token="",
        speech_analysis_llm_base_url="https://model.invalid/v1",
        speech_analysis_llm_api_key="model-secret",
        speech_analysis_llm_model="production-model",
        speech_analysis_llm_candidate_model="candidate-model",
        speech_analysis_llm_free_model="free-model",
    )


def _transport(monkeypatch, handler) -> None:
    """Подставить только транспорт; сохранить реальный HTTP-клиент и подготовку."""
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *args, **kwargs: original(*args, transport=transport, **kwargs)
    )


@pytest.mark.parametrize(
    "scenario,profile,model",
    [
        ("analysis", "production", "production-model"),
        ("reason", "production", "production-model"),
        ("free_classification", "production", "free-model"),
        ("operator_compare", "production", "free-model"),
        ("analysis", "candidate", "candidate-model"),
        ("reason", "candidate", "candidate-model"),
    ],
)
async def test_local_scenarios_need_only_model_credentials(monkeypatch, scenario, profile, model):
    """Все сохранённые сценарии работают локально; транспорт видит только ключ модели."""
    requests = []

    def respond(request):
        assert request.url == "https://model.invalid/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer model-secret"
        body = json.loads(request.content)
        assert body["model"] == model
        assert body["messages"] and body["max_tokens"] > 0
        requests.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    _transport(monkeypatch, respond)
    client = build_analysis_client(_settings(), scenario=scenario, profile=profile)
    assert await client.run(_inputs(scenario)) == {"answer": "answer"}
    assert len(requests) == 1


async def test_reason_command_uses_backend_reference_and_validation(monkeypatch):
    """Реальный клиент причин получает локальный промпт; код проверяет бизнес-сценарий."""

    def respond(request):
        body = json.loads(request.content)
        assert "K05" in body["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"call_reason": "Нет связи.", "call_reason_code": "К05 · подпись"}
                            )
                        }
                    }
                ]
            },
        )

    _transport(monkeypatch, respond)
    result = await ask_reason(
        build_reason_client(_settings()),
        uuid4(),
        "K05 · Нет связи",
        frozenset({"K05"}),
        (AnalysisSegment(seq=1, speaker="client", text_raw="Нет связи."),),
    )
    assert result is not None and result.code == "K05"


def _sample() -> tuple[Sampled, AnalysisInput]:
    """Разговор с двумя репликами и включённой веткой классификатора."""
    call_id, root_id, product_id = uuid4(), uuid4(), uuid4()
    return (
        Sampled(
            call_id=call_id,
            operator_node_id=product_id,
            ai_node_id=product_id,
            sentiment="negative",
            reason_code=None,
        ),
        AnalysisInput(
            call=CallForAnalysis(id=call_id, role_method="none"),
            segments=(
                AnalysisSegment(seq=1, speaker="unknown", text_raw="Первоначальная реплика."),
                AnalysisSegment(seq=2, speaker="unknown", text_raw="Ответ клиента."),
            ),
            classifier=(
                ClassifierRecord(
                    id=root_id, parent_id=None, level=1, label="Физ.лицо", key=label_key("Физ.лицо")
                ),
                ClassifierRecord(
                    id=product_id, parent_id=root_id, level=2, label="ШПД", key=label_key("ШПД")
                ),
            ),
            reasons=(),
        ),
    )


async def test_comparison_reuses_one_input_for_candidate_and_control(monkeypatch):
    """Изменение хранилища между запросами не меняет вход второй модели."""
    call, data = _sample()
    repository = AsyncMock(analysis_input=AsyncMock(return_value=data))
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        repository.analysis_input.return_value = None
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(_answer())}}]}
        )

    _transport(monkeypatch, respond)
    rows = await compare(
        [call],
        repository=repository,
        candidate=build_candidate_client(_settings()),
        control=build_analysis_client(_settings()),
        concurrency=1,
        echo=lambda _line: None,
    )
    repository.analysis_input.assert_awaited_once_with(call.call_id)
    assert len(requests) == 2
    assert requests[0]["messages"] == requests[1]["messages"]
    assert {body["model"] for body in requests} == {"candidate-model", "production-model"}
    assert rows[0].candidate is not None and rows[0].candidate.work is not None
    assert rows[0].control is not None and rows[0].control.work == rows[0].candidate.work


async def test_absent_comparison_input_never_calls_models():
    """Пропавший разговор даёт отказ в строке отчёта без LLM и записи."""
    call, _ = _sample()
    model = AsyncMock()
    rows = await compare(
        [call],
        repository=AsyncMock(analysis_input=AsyncMock(return_value=None)),
        candidate=model,
        control=None,
        concurrency=1,
        echo=lambda _line: None,
    )
    assert rows[0].candidate is not None and rows[0].candidate.error is not None
    assert rows[0].candidate.error.startswith("not_found:")
    model.run.assert_not_awaited()


async def test_comparison_failure_does_not_expose_provider_text():
    """Сетевой отказ не останавливает отчёт и не раскрывает исключение провайдера."""
    call, data = _sample()
    error = AnalysisError("timeout", retryable=True)
    error.__cause__ = RuntimeError("PRIVATE_PROVIDER_BODY")
    rows = await compare(
        [call],
        repository=AsyncMock(analysis_input=AsyncMock(return_value=data)),
        candidate=AsyncMock(run=AsyncMock(side_effect=error)),
        control=None,
        concurrency=1,
        echo=lambda _line: None,
    )
    assert rows[0].candidate is not None
    assert rows[0].candidate.error is not None
    assert rows[0].candidate.error.startswith("timeout:")
    assert "PRIVATE_PROVIDER_BODY" not in rows[0].model_dump_json()
