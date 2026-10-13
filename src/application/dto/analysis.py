"""Вход и результат самостоятельного разбора ответа модели анализа."""

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from src.application.dto.pipeline_input import ClassifierRecord
from src.domain.calls.analysis import AnalyzeWork as AnalyzeWork


class AnalysisInterpretationContext(BaseModel):
    """Снимок дерева, необходимость ролей и коды причин, отправленные модели."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    classifier: tuple[ClassifierRecord, ...]
    needs_roles: bool
    reason_codes: frozenset[str] = frozenset()


class AnalysisInterpretationRequest(BaseModel):
    """Ответ модели в JSON-совместимом виде и контекст его проверки."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    outputs: JsonValue = Field(repr=False)
    context: AnalysisInterpretationContext
