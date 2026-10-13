"""Самостоятельный разбор через DTO: сериализация, отказы и сохранение контекста."""

import json
from uuid import UUID

import pytest
from pydantic import JsonValue, ValidationError

from src.application.analysis.interpretation import AnalysisInterpretationService
from src.application.dto.analysis import (
    AnalysisInterpretationContext,
    AnalysisInterpretationRequest,
    AnalyzeWork,
)
from src.application.dto.pipeline_input import ClassifierRecord
from src.application.errors.analysis import InvalidAnalysisOutput
from src.domain.calls.classifier import label_key
from src.domain.calls.contracts import Speaker

ROOT_ID = UUID("00000000-0000-0000-0000-000000000001")
PRODUCT_ID = UUID("00000000-0000-0000-0000-000000000002")
PRIVATE_TEXT = "Содержание разговора, которое не должно попасть в ошибку"


@pytest.fixture
def context() -> AnalysisInterpretationContext:
    """Вернуть снимок включённой ветки и кодов, отправленных модели."""
    return AnalysisInterpretationContext(
        classifier=(
            ClassifierRecord(
                id=ROOT_ID, parent_id=None, level=1, label="Физ.лицо", key=label_key("Физ.лицо")
            ),
            ClassifierRecord(
                id=PRODUCT_ID, parent_id=ROOT_ID, level=2, label="ШПД", key=label_key("ШПД")
            ),
        ),
        needs_roles=True,
        reason_codes=frozenset({"K05"}),
    )


def _answer(**updates: JsonValue) -> dict[str, JsonValue]:
    """Вернуть ответ с необязательными полями и двумя предложенными ролями."""
    return {
        "path": ["Физ.лицо", "ШПД"],
        "sentiment": "negative",
        "summary": "  Резюме.  ",
        "roles": [{"seq": 1, "speaker": "operator"}, {"seq": 2, "speaker": "client"}],
        "call_reason": "Не работает интернет.",
        "call_reason_code": "К05 · подпись",
        **updates,
    }


@pytest.mark.parametrize("key", [None, "answer", "text", "output"])
def test_serialized_dto_roundtrip_returns_the_same_complete_result(context, key) -> None:
    """Уже разобранный объект и текстовые обёртки дают одинаковый сериализуемый итог."""
    answer = _answer()
    outputs = answer if key is None else {key: json.dumps(answer, ensure_ascii=False)}
    command = AnalysisInterpretationRequest(outputs=outputs, context=context)
    command = AnalysisInterpretationRequest.model_validate_json(command.model_dump_json())
    result = AnalysisInterpretationService().interpret(command)

    assert result.node_id == PRODUCT_ID
    assert result.path == ("Физ.лицо", "ШПД")
    assert result.summary == "Резюме."
    assert result.roles == ((1, Speaker.OPERATOR), (2, Speaker.CLIENT))
    assert (result.call_reason, result.call_reason_code) == ("Не работает интернет.", "K05")
    assert AnalyzeWork.model_validate_json(result.model_dump_json()) == result


def test_service_reads_final_json_after_reasoning_and_markdown(context) -> None:
    """Черновой JSON внутри рассуждения не заменяет окончательный ответ анализа."""
    raw = '<think>{"path":["Выдумка"],"sentiment":"neutral"}</think>\n'
    raw += "```json\n" + json.dumps(_answer(), ensure_ascii=False) + "\n```"
    result = AnalysisInterpretationService().interpret(
        AnalysisInterpretationRequest(outputs={"answer": raw}, context=context)
    )
    assert result.node_id == PRODUCT_ID
    assert result.sentiment is not None and result.sentiment.value == "negative"


@pytest.mark.parametrize(
    "outputs",
    [
        {"answer": PRIVATE_TEXT},
        {"answer": "<think>" + PRIVATE_TEXT},
        _answer(path=[]),
        _answer(sentiment=PRIVATE_TEXT),
    ],
)
def test_invalid_answer_has_normalized_failure_without_input_text(context, outputs) -> None:
    """Отказ пригоден для повторов и логов; исходный ответ не включается в сообщение."""
    command = AnalysisInterpretationRequest(outputs=outputs, context=context)
    with pytest.raises(InvalidAnalysisOutput) as failure:
        AnalysisInterpretationService().interpret(command)
    assert PRIVATE_TEXT not in str(failure.value)
    assert PRIVATE_TEXT not in repr(command)


def test_known_roles_ignore_even_an_unreadable_optional_roles_field(context) -> None:
    """Когда роли известны, содержимое необязательного поля не влияет на разбор."""
    result = AnalysisInterpretationService().interpret(
        AnalysisInterpretationRequest(
            outputs=_answer(roles=True), context=context.model_copy(update={"needs_roles": False})
        )
    )
    assert result.roles == ()
    assert result.node_id == PRODUCT_ID


def test_reused_service_does_not_cache_tree_or_mutate_request(context) -> None:
    """Новый снимок дерева применяется сразу, а старый DTO остаётся прежним."""
    service = AnalysisInterpretationService()
    command = AnalysisInterpretationRequest(outputs=_answer(), context=context)
    before = command.model_dump_json()
    assert service.interpret(command).node_id == PRODUCT_ID
    disabled = context.classifier[0].model_copy(update={"is_active": False})
    updated = context.model_copy(update={"classifier": (disabled, context.classifier[1])})
    result = service.interpret(AnalysisInterpretationRequest(outputs=_answer(), context=updated))
    assert (result.node_id, result.path) == (None, ())
    assert command.model_dump_json() == before


def test_dto_rejects_undeclared_context_fields(context) -> None:
    """Ошибочное имя поля не должно незаметно изменить применение бизнес-правил."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AnalysisInterpretationContext.model_validate(
            {**context.model_dump(), "replace_roles": True}
        )
