"""Локальная постановка оценки бэкендом; внешних команд обслуживания здесь нет."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.application.dto.quality_batch import RunEvaluation


class QualityQueueRequest(BaseModel):
    """Проверка охвата либо постановка оценки в очередь; сеть модели здесь не нужна."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    contract_version: Literal["1.0"] = "1.0"
    selection: RunEvaluation


class QualityQueueReport(BaseModel):
    """Число кандидатов, реально поставленных заданий либо устаревших оценок."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    contract_version: Literal["1.0"] = "1.0"
    candidates: int = Field(default=0, ge=0)
    enqueued: int = Field(default=0, ge=0)
    stale: int | None = Field(default=None, ge=0)
    applied: bool = False
