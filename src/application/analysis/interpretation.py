"""Самостоятельный сценарий проверки и интерпретации ответа анализа звонка."""

from typing import Any

from src.application.analysis.output import describe_raw_problem, normalise_outputs, require_shape
from src.application.dto.analysis import AnalysisInterpretationRequest, AnalyzeWork
from src.application.errors.analysis import InvalidAnalysisOutput
from src.domain.calls.analysis import AnalysisAnswer, RoleCandidate, interpret_analysis
from src.domain.calls.classifier import ClassifierIndex
from src.domain.calls.contracts import Sentiment


class AnalysisInterpretationService:
    """Применить правила анализа к одному ответу без базы, модели и очереди."""

    def interpret(self, command: AnalysisInterpretationRequest) -> AnalyzeWork:
        """Вернуть готовый результат по DTO ответа и снимка контекста.

        Читает JSON, проверяет обязательные поля и применяет domain-правила.
        Непригодный ответ вызывает InvalidAnalysisOutput. Не вызывает LLM,
        не записывает данные и не решает, повторять ли техническое задание.
        """
        problem = describe_raw_problem(command.outputs)
        if problem:
            raise InvalidAnalysisOutput(problem)
        outputs = normalise_outputs(command.outputs)
        require_shape(outputs)
        answer = _read_answer(outputs, needs_roles=command.context.needs_roles)
        classifier = ClassifierIndex(node.model_dump() for node in command.context.classifier)
        return interpret_analysis(
            answer,
            classifier=classifier,
            needs_roles=command.context.needs_roles,
            reason_codes=command.context.reason_codes,
        )


def _read_answer(outputs: dict[str, Any], *, needs_roles: bool) -> AnalysisAnswer:
    """Прочитать проверенный объект в доменную модель; лишние поля не переносить."""
    roles: list[RoleCandidate] = []
    for item in (outputs.get("roles") or []) if needs_roles else ():
        if not isinstance(item, dict):
            continue
        try:
            seq = int(item["seq"])
        except (KeyError, TypeError, ValueError):
            seq = None
        roles.append(RoleCandidate(seq=seq, speaker=str(item.get("speaker") or "")))
    return AnalysisAnswer(
        path=_read_levels(outputs["path"]),
        sentiment=Sentiment(outputs["sentiment"]),
        summary=_read_text(outputs.get("summary")),
        roles=tuple(roles),
        suggestion=_read_levels(outputs.get("ai_path")),
        call_reason=_read_text(outputs.get("call_reason")),
        call_reason_code=_read_text(outputs.get("call_reason_code")),
    )


def _read_levels(value: Any) -> tuple[str | None, ...]:
    """Прочитать подписи из списка по местам; нестроковые значения оставить пустыми."""
    if not isinstance(value, list):
        return ()
    return tuple(item if isinstance(item, str) else None for item in value)


def _read_text(value: Any) -> str | None:
    """Вернуть строковое значение без правок; остальные типы считать отсутствием текста."""
    return value if isinstance(value, str) else None
