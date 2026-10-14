"""Сведения фактического запроса относятся к проверенному результату анализа."""

import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import JsonValue, ValidationError

from src.application.analysis.execution import AnalysisExecutionService
from src.application.analysis.interpretation import ANALYSIS_RULES_VERSION
from src.application.dto.analysis_computation import AnalysisComputation
from src.application.dto.model_execution import ModelExecutionMetadata, ModelExecutionResult
from src.di.analysis import build_analysis_client
from src.domain.calls.reasons import CallReason
from tests.model_execution_support import model_result
from tests.test_analysis_interpretation import _answer
from tests.test_auxiliary_model_execution import _sample, _settings, _transport


async def test_validated_result_keeps_metadata_of_the_successful_http_call(monkeypatch):
    """После плохого ответа сохраняются UUID и факты следующего пригодного вызова."""
    call, data = _sample()
    sent = []

    def respond(request):
        sent.append(request)
        content = "Не удалось ответить JSON" if len(sent) == 1 else json.dumps(_answer())
        return httpx.Response(
            200,
            json={
                "model": f"served-{len(sent)}",
                "choices": [{"message": {"content": content}}],
            },
        )

    _transport(monkeypatch, respond)
    client = build_analysis_client(_settings())
    responses: list[ModelExecutionResult] = []

    class RecordingBackend:
        """Запомнить DTO настоящего HTTP-клиента без изменения ответа и метаданных."""

        async def run(self, inputs: dict[str, JsonValue]) -> ModelExecutionResult:
            """Вызвать настоящий клиент с подставным HTTP и сохранить выданный DTO."""
            result = await client.run(inputs)
            responses.append(result)
            return result

    result = await AnalysisExecutionService().execute(call.call_id, data, RecordingBackend())
    assert result.work.summary == "Резюме."
    assert result.model_call_index == 2
    assert result.rules_version == ANALYSIS_RULES_VERSION
    assert result.execution == responses[-1].execution
    assert result.execution.execution_id != responses[0].execution.execution_id
    assert result.execution.requested_model == "production-model"
    assert result.execution.served_model == "served-2"
    canonical = json.dumps(
        json.loads(sent[-1].content), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert result.execution.request_fingerprint == hashlib.sha256(canonical.encode()).hexdigest()
    assert result.reference.reason_reference_state == "empty"
    assert {node.id for node in result.reference.classifier} == {
        node.id for node in data.classifier
    }
    assert data.segments[0].text_raw not in result.execution.model_dump_json()
    assert result.work.summary is not None and result.work.summary not in repr(result)
    assert AnalysisComputation.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("state", ["sent", "empty", "omitted_too_large"])
async def test_computation_records_only_the_reason_reference_available_to_model(monkeypatch, state):
    """Отсутствующие и слишком большие причины отличимы от отправленного справочника."""
    call, data = _sample()
    if state != "empty":
        data = data.model_copy(
            update={
                "reasons": (
                    CallReason(
                        code="K05",
                        label="Нет связи",
                        hint="Правило" if state == "sent" else "я" * 16001,
                    ),
                )
            }
        )
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(_answer())}}]}
        )

    _transport(monkeypatch, respond)
    result = await AnalysisExecutionService().execute(
        call.call_id, data, build_analysis_client(_settings())
    )
    assert result.reference.reason_reference_state == state
    assert [reason.code for reason in result.reference.reasons] == (
        ["K05"] if state == "sent" else []
    )
    assert ("K05" in sent[0]["messages"][-1]["content"]) == (state == "sent")


@pytest.mark.parametrize(
    "change",
    [
        {"prompt_digest": "invalid"},
        {"request_fingerprint": "A" * 64},
        {"max_tokens": True},
        {"started_at": "2026-10-14T00:00:00"},
        {"api_key": "PRIVATE_SECRET"},
        {"raw_response": "PRIVATE_RESPONSE"},
    ],
)
def test_execution_metadata_rejects_invalid_or_undeclared_fields(change):
    """Порт не принимает недостоверные хеши, время без зоны и произвольные секреты."""
    metadata = model_result({}).execution
    with pytest.raises(ValidationError):
        ModelExecutionMetadata.model_validate({**metadata.model_dump(), **change})


def test_execution_metadata_rejects_response_before_request():
    """Некорректное время не становится доказательством выполненного запроса."""
    metadata = model_result({}).execution
    with pytest.raises(ValidationError):
        ModelExecutionMetadata.model_validate(
            {**metadata.model_dump(), "finished_at": metadata.started_at - timedelta(seconds=1)}
        )
