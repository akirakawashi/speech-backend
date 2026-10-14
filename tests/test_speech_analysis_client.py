"""Прямой вызов LLM: подготовленный запрос, ответ и решения о повторе."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import JsonValue

from src.application.dto.model_execution import (
    ModelMessage,
    ModelPreparationRequest,
    PreparedModelRequest,
)
from src.application.errors.external import AnalysisError
from src.infrastructure.clients.analysis import SpeechAnalysisClient


class _Preparer:
    """Запомнить исходный DTO и вернуть готовый запрос без внешних сервисов."""

    def __init__(self) -> None:
        self.requests: list[ModelPreparationRequest] = []

    async def prepare(self, request: ModelPreparationRequest) -> PreparedModelRequest:
        self.requests.append(request)
        return PreparedModelRequest(
            contract_version="1.0",
            scenario=request.scenario,
            profile=request.profile,
            prompt_version="1.0",
            prompt_digest="a" * 64,
            model="selected-by-backend",
            messages=(ModelMessage(role="user", content="Разговор: здравствуйте"),),
            temperature=0,
            max_tokens=8192,
        )


def _transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[..., Any]) -> None:
    """Подставить HTTP-транспорт; сериализацию настоящего httpx сохранить."""
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_sends_backend_request_unchanged_and_returns_model_content(monkeypatch) -> None:
    """Модель и сообщения выбирает preparer, ответ остаётся под ключом answer."""
    sent: list[httpx.Request] = []
    answer = '{"summary":"Клиент позвонил"}'

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": answer}}]}
        )

    _transport(monkeypatch, handler)
    preparer = _Preparer()
    inputs: dict[str, JsonValue] = {"segments": [{"text": "Здравствуйте"}], "needs_roles": True}
    client = SpeechAnalysisClient(
        base_url="http://model/v1",
        api_key="model-secret",
        preparer=preparer,
        scenario="reason",
        profile="candidate",
    )
    started = datetime.now(UTC)
    result = await client.run(inputs)
    finished = datetime.now(UTC)
    assert result.outputs == {"answer": answer}
    metadata = result.execution
    assert metadata.scenario == "reason" and metadata.profile == "candidate"
    assert metadata.requested_model == "selected-by-backend"
    assert metadata.served_model is None and metadata.model_digest is None
    assert metadata.prompt_version == "1.0" and metadata.prompt_digest == "a" * 64
    assert metadata.temperature == 0 and metadata.max_tokens == 8192
    assert started <= metadata.started_at <= metadata.finished_at <= finished
    assert preparer.requests == [
        ModelPreparationRequest(scenario="reason", profile="candidate", inputs=inputs)
    ]
    assert str(sent[0].url) == "http://model/v1/chat/completions"
    assert sent[0].headers["authorization"] == "Bearer model-secret"
    assert json.loads(sent[0].content) == {
        "model": "selected-by-backend",
        "messages": [{"role": "user", "content": "Разговор: здравствуйте"}],
        "temperature": 0,
        "max_tokens": 8192,
    }
    canonical = json.dumps(
        json.loads(sent[0].content), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert metadata.request_fingerprint == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert "Здравствуйте" not in metadata.model_dump_json()
    assert answer not in repr(result)
    assert "model-secret" not in result.model_dump_json()
    assert "model-secret" not in repr(client)


async def test_local_model_may_have_no_api_key(monkeypatch) -> None:
    """Локальный сервер модели без авторизации не получает пустой Bearer."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    _transport(monkeypatch, handler)
    await SpeechAnalysisClient(base_url="http://model/v1", preparer=_Preparer()).run({})


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(408, True), (429, True), (500, True), (503, True), (400, False), (401, False), (404, False)],
)
async def test_http_failure_has_retry_policy_without_response_body(monkeypatch, status, retryable):
    """Секреты из чужого тела ошибки не попадают в ошибку задания."""
    _transport(monkeypatch, lambda request: httpx.Response(status, text="sensitive-model-response"))
    with pytest.raises(AnalysisError) as error:
        await SpeechAnalysisClient(base_url="http://model/v1", preparer=_Preparer()).run({})
    assert error.value.reason == f"http_{status}"
    assert error.value.retryable is retryable
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
async def test_network_failure_can_be_retried(monkeypatch, failure):
    """Истечение таймаута и недоступный сервер допускают повтор задания."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("temporary failure", request=request)

    _transport(monkeypatch, handler)
    with pytest.raises(AnalysisError) as error:
        await SpeechAnalysisClient(base_url="http://model/v1", preparer=_Preparer()).run({})
    assert error.value.retryable


@pytest.mark.parametrize(
    ("body", "reason", "retryable"),
    [
        ([], "malformed_response", True),
        ({}, "malformed_response", True),
        ({"choices": []}, "malformed_response", True),
        ({"choices": [None]}, "malformed_response", True),
        ({"choices": [{"message": None}]}, "malformed_response", True),
        ({"choices": [{"message": {"content": None}}]}, "empty_response", True),
        ({"choices": [{"message": {"content": " "}}]}, "empty_response", True),
        ({"choices": [{"finish_reason": "length"}]}, "output_truncated", False),
        ({"choices": [{"message": {"refusal": "refused"}}]}, "model_refused", False),
    ],
)
async def test_malformed_empty_truncated_and_refused_responses(
    monkeypatch, body, reason, retryable
):
    """Сбой протокола отличается от отказа модели и недостаточного лимита."""
    _transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(AnalysisError) as error:
        await SpeechAnalysisClient(base_url="http://model/v1", preparer=_Preparer()).run({})
    assert (error.value.reason, error.value.retryable) == (reason, retryable)


async def test_non_json_response_is_retryable(monkeypatch):
    """Прокси может ответить HTML вместо JSON; тело не журналируем."""
    _transport(monkeypatch, lambda request: httpx.Response(200, text="<html>error</html>"))
    with pytest.raises(AnalysisError) as error:
        await SpeechAnalysisClient(base_url="http://model/v1", preparer=_Preparer()).run({})
    assert (error.value.reason, error.value.retryable) == ("malformed_response", True)


async def test_preparation_failure_does_not_call_model(monkeypatch):
    """При отказе подготовки запрос к модели не выполняется."""

    class FailingPreparer:
        async def prepare(self, request: ModelPreparationRequest) -> PreparedModelRequest:
            raise AnalysisError("invalid_model_request", retryable=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("model called after preparation failure")

    _transport(monkeypatch, handler)
    with pytest.raises(AnalysisError, match="invalid_model_request"):
        await SpeechAnalysisClient(base_url="http://model/v1", preparer=FailingPreparer()).run({})


@pytest.mark.parametrize(
    "reported,expected",
    [
        (
            {"model": "served-alias", "model_digest": "sha256:weights"},
            ("served-alias", "sha256:weights"),
        ),
        ({}, (None, None)),
        ({"model": " ", "model_digest": ""}, (None, None)),
        ({"model": 42, "model_digest": {"digest": "wrong shape"}}, (None, None)),
        ({"system_fingerprint": "provider-internal-id"}, (None, None)),
    ],
)
async def test_provider_identity_is_taken_only_from_explicit_protocol_fields(
    monkeypatch, reported, expected
):
    """Имя из запроса, поля протокола и выдуманные LLM сведения не смешиваются."""
    answer = json.dumps({"model": "invented-model", "model_digest": "invented-digest"})
    _transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={**reported, "choices": [{"message": {"content": answer}}]}
        ),
    )
    result = await SpeechAnalysisClient(base_url="http://model/v1", preparer=_Preparer()).run({})
    assert result.execution.requested_model == "selected-by-backend"
    assert (result.execution.served_model, result.execution.model_digest) == expected


async def test_each_http_call_has_its_own_identity_with_a_stable_request_hash(monkeypatch):
    """Повтор одинакового запроса сохраняет хеш, но получает самостоятельный UUID."""
    _transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}),
    )
    client = SpeechAnalysisClient(base_url="http://model/v1", preparer=_Preparer())
    first, second = await client.run({}), await client.run({})
    assert first.execution.execution_id != second.execution.execution_id
    assert first.execution.request_fingerprint == second.execution.request_fingerprint
    assert first.execution.finished_at <= second.execution.started_at


async def test_configuration_change_during_http_does_not_rewrite_request_facts(monkeypatch):
    """Замена шаблона/модели во время ожидания влияет только на следующий вызов."""
    original = await _Preparer().prepare(ModelPreparationRequest(scenario="analysis", inputs={}))

    class ChangingPreparer:
        """Изобразить замену конфигурации между подготовкой и получением ответа."""

        current = original

        async def prepare(self, request: ModelPreparationRequest) -> PreparedModelRequest:
            """Вернуть текущий неизменяемый DTO запроса."""
            return self.current

    preparer = ChangingPreparer()
    replacement = original.model_copy(
        update={
            "model": "next-model",
            "prompt_version": "next-prompt",
            "prompt_digest": "b" * 64,
            "temperature": 0.5,
            "max_tokens": 4096,
            "messages": (ModelMessage(role="user", content="Следующая инструкция"),),
        }
    )
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        preparer.current = replacement
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    _transport(monkeypatch, respond)
    client = SpeechAnalysisClient(base_url="http://model/v1", preparer=preparer)
    first, second = await client.run({}), await client.run({})
    assert preparer.current == replacement
    assert first.execution.requested_model == sent[0]["model"] == original.model
    assert first.execution.prompt_version == original.prompt_version
    assert first.execution.prompt_digest == original.prompt_digest
    assert first.execution.temperature == original.temperature
    assert first.execution.max_tokens == original.max_tokens
    assert second.execution.requested_model == sent[1]["model"] == replacement.model
    assert second.execution.prompt_version == replacement.prompt_version
    assert second.execution.prompt_digest == replacement.prompt_digest
    assert first.execution.request_fingerprint != second.execution.request_fingerprint
