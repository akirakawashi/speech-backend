"""Политика бэкенда: входы промптов, версии, выбор модели и защита контракта."""

import json
from typing import Any

import pytest
from pydantic import JsonValue

from src.application.dto.model_execution import ModelPreparationRequest
from src.application.errors.external import AnalysisNotConfigured
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.model_requests.service import ModelRequestService
from src.di.analysis import build_analysis_client
from src.di.model_requests import build_model_request_service
from src.infrastructure.prompts.catalogue import FilePromptCatalogue
from src.setting.config import Settings


def _service(**changes: Any) -> ModelRequestService:
    """Собрать настоящую политику с локальными ресурсами и явными настройками."""
    return build_model_request_service(
        Settings(
            _env_file=None,
            speech_analysis_llm_model="production-model",
            speech_analysis_llm_free_model="free-model",
            speech_analysis_llm_candidate_model="candidate-model",
            speech_worker_api_token="worker-secret",
        ).model_copy(update=changes)
    )


def _inputs(scenario: str) -> dict[str, JsonValue]:
    """Минимальные корректные данные для каждого существующего сценария."""
    data: dict[str, JsonValue] = {
        "call_id": "c-1",
        "segments": [{"seq": 1, "text": "Здравствуйте", "speaker": "unknown"}],
    }
    if scenario == "analysis":
        data.update(needs_roles=True, classifier={"Интернет": {}})
    if scenario == "reason":
        data["reasons"] = ["Нет интернета"]
    if scenario == "operator_compare":
        return {
            "call_id": "c-1",
            "operator_path": ["Интернет"],
            "free_path": ["Связь"],
            "topic": "Не работает интернет",
            "candidates": [["Интернет"]],
        }
    return data


@pytest.mark.parametrize(
    ("scenario", "model"),
    [
        ("analysis", "production-model"),
        ("reason", "production-model"),
        ("free_classification", "free-model"),
        ("operator_compare", "free-model"),
    ],
)
@pytest.mark.parametrize("profile", ["production", "candidate"])
async def test_backend_selects_model_and_version(scenario, model, profile):
    """Исполнитель получает выбор бэкенда, версии и параметры старого промпта."""
    result = await _service().prepare(
        ModelPreparationRequest(scenario=scenario, profile=profile, inputs=_inputs(scenario))
    )
    assert result.contract_version == "1.0"
    assert result.prompt_version == "1.0"
    assert result.model == ("candidate-model" if profile == "candidate" else model)
    assert result.temperature == 0
    assert result.max_tokens == 8192
    assert result.messages


async def test_unicode_json_flags_and_optional_reason_reference():
    """Структуры сохраняют JSON и кириллицу, bool превращается в 1, причины необязательны."""
    result = await _service().prepare(
        ModelPreparationRequest(scenario="analysis", inputs=_inputs("analysis"))
    )
    user = next(m.content for m in result.messages if m.role == "user")
    assert "Здравствуйте" in user
    assert json.dumps(_inputs("analysis")["segments"], ensure_ascii=False) in user
    assert json.dumps({"Интернет": {}}, ensure_ascii=False) in user
    assert "needs_roles = 1" in user
    assert "{{reasons}}" not in user
    assert "Здравствуйте" not in repr(result)


async def test_variables_in_conversation_are_not_rendered_as_template():
    """Текст звонка подставляется один раз: похожие на шаблон слова остаются данными."""
    data = _inputs("analysis")
    data["segments"] = "{{classifier}} {{needs_roles}}"
    result = await _service().prepare(ModelPreparationRequest(scenario="analysis", inputs=data))
    assert "{{classifier}} {{needs_roles}}" in result.messages[-1].content


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": "sensitive input"},
        {"segments": None},
        {"segments": ""},
        {"segments": "sensitive" * 70_000},
        {"call_id": "c" * 65},
        {"classifier": "sensitive" * 6_000},
        {"reasons": "sensitive" * 2_000},
    ],
)
async def test_invalid_input_has_bounded_error_without_values(change):
    """Неизвестные и слишком длинные входы отклоняются без возврата текста звонка."""
    with pytest.raises(ApplicationError) as error:
        await _service().prepare(
            ModelPreparationRequest(scenario="analysis", inputs={**_inputs("analysis"), **change})
        )
    assert error.value.kind == ErrorKind.INVALID_INPUT
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("model", ["", "production-model"])
async def test_candidate_must_be_configured_and_different(model):
    """Сравнение не выдаёт обычную модель за кандидата."""
    service = _service(speech_analysis_llm_candidate_model=model)
    with pytest.raises(ApplicationError):
        await service.prepare(
            ModelPreparationRequest(
                scenario="analysis", profile="candidate", inputs=_inputs("analysis")
            )
        )


@pytest.mark.parametrize("header", [None, "", "Basic worker-secret", "Bearer wrong-secret"])
async def test_service_requires_worker_token(header):
    """Пользовательская сессия не заменяет служебный токен."""
    with pytest.raises(ApplicationError) as error:
        await _service().prepare_authorized(
            ModelPreparationRequest(scenario="analysis", inputs=_inputs("analysis")), header
        )
    assert error.value.kind == ErrorKind.UNAUTHORIZED


async def test_unconfigured_token_closes_private_route():
    """Пустая настройка закрывает служебную подготовку, а не открывает всем доступ."""
    with pytest.raises(ApplicationError) as error:
        await _service(speech_worker_api_token="").prepare_authorized(
            ModelPreparationRequest(scenario="analysis", inputs=_inputs("analysis")), None
        )
    assert error.value.kind == ErrorKind.UNAVAILABLE


def test_cli_can_prepare_locally_but_requires_llm_address():
    """Старый CLI в бэкенде использует ту же политику без HTTP-вызова самому себе."""
    settings = Settings(_env_file=None, speech_analysis_llm_base_url="")
    with pytest.raises(AnalysisNotConfigured):
        build_analysis_client(settings)
    client = build_analysis_client(
        settings.model_copy(update={"speech_analysis_llm_base_url": "http://model/v1/"})
    )
    assert client.base_url == "http://model/v1"


def test_prompt_inputs_keep_existing_bounds():
    """Точные ограничения сохраняют защиту от слишком больших запросов."""
    template = FilePromptCatalogue().get("analysis")
    limits = {v.name: v.max_length for v in template.variables}
    assert limits == {
        "call_id": 64,
        "needs_roles": None,
        "classifier": 48000,
        "reasons": 16000,
        "segments": 480000,
    }
    assert not next(v for v in template.variables if v.name == "reasons").required
