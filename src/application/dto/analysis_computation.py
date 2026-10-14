"""Проверенный результат анализа вместе с фактами его вычисления."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.application.dto.analysis import AnalyzeWork
from src.application.dto.analysis_storage import AnalysisReferencePayload
from src.application.dto.model_execution import ModelExecutionMetadata


class AnalysisComputation(BaseModel):
    """Один пригодный ответ, его запрос и справочники до записи результата.

    Номер вызова относится к попыткам разбора внутри одного вычисления,
    а не к номеру захвата задания. Неудачные ответы сюда не входят.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    work: AnalyzeWork = Field(repr=False)
    execution: ModelExecutionMetadata
    reference: AnalysisReferencePayload = Field(repr=False)
    rules_version: str = Field(min_length=1)
    model_call_index: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def validate_scenario(self) -> Self:
        """Вернуть вычисление анализа; другой сценарий вызывает ValueError."""
        if self.execution.scenario != "analysis" or self.reference.scenario != "analysis":
            raise ValueError("analysis computation requires an analysis request and reference")
        return self
