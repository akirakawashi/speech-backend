"""Чтение JSON-ответа анализа и проверка обязательных полей без бизнес-умолчаний."""

import re
from typing import Any, Final

from src.application.errors.analysis import InvalidAnalysisOutput
from src.application.model_output import _json_object
from src.domain.calls.contracts import Sentiment

_RAW_ANSWER_KEYS: Final[tuple[str, ...]] = ("answer", "text", "output")
_THINK_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE
)


def describe_raw_problem(outputs: Any) -> str | None:
    """Вернуть безопасную причину пустого/оборванного ответа либо None.

    Принимает JSON-совместимый выход модели. Проверяет текстовые обёртки до
    чтения JSON; не включает содержимое ответа в диагностическое сообщение.
    """
    if not isinstance(outputs, dict) or "path" in outputs:
        return None
    for key in _RAW_ANSWER_KEYS:
        raw = outputs.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        stripped = _THINK_BLOCK.sub("", raw)
        if "<think" in stripped.lower():
            return (
                f"the model ran out of output tokens while reasoning "
                f"({len(raw)} characters, no closing </think>): raise max_tokens"
            )
        if "{" not in stripped:
            return "the model answered without a JSON object"
        return None
    return "the model returned no answer at all"


def normalise_outputs(outputs: Any) -> dict[str, Any]:
    """Прочитать текст из answer/text/output либо вернуть уже разобранный объект.

    Снимает поддерживаемые обёртки через общий читатель JSON. Возвращает
    словарь, в том числе пустой при непрочитанном ответе; его пригодность
    проверяет сценарий. Уже разобранный объект не изменяет.
    """
    if not isinstance(outputs, dict):
        return {}
    if "path" in outputs:
        return outputs
    for key in _RAW_ANSWER_KEYS:
        raw = outputs.get(key)
        if isinstance(raw, str) and raw.strip():
            return _json_object(raw) or {}
    return outputs


def require_shape(outputs: Any) -> None:
    """Проверить объект, непустой путь и одну из трёх тональностей.

    Принимает прочитанный объект. Возвращает None при допустимой форме,
    иначе поднимает InvalidAnalysisOutput без значений полей. Не проверяет
    наличие пути в дереве или необязательного предложения темы.
    """
    if not isinstance(outputs, dict):
        raise InvalidAnalysisOutput("outputs must be an object")
    path = outputs.get("path")
    if not isinstance(path, list) or not any(
        isinstance(level, str) and level.strip() for level in path
    ):
        raise InvalidAnalysisOutput("path is missing")
    sentiment = outputs.get("sentiment")
    if not isinstance(sentiment, str) or sentiment not in {s.value for s in Sentiment}:
        raise InvalidAnalysisOutput("sentiment is not one of the three known values")
