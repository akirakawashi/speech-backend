"""Типизированные команды и проекции операций reports."""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from src.application.authorization import Principal
from src.application.dto.api import Envelope
from src.application.dto.calls import CallSource


class ClassificationNode(BaseModel):
    """Узел классификатора, который в периоде встречается, — вариант фильтра отчёта."""

    id: uuid.UUID
    parent_id: uuid.UUID | None = None
    level: int
    label: str
    sort_order: int
    operator_calls: int = 0
    ai_calls: int = 0


class ClassificationResponse(Envelope):
    """Результат построения отчёта по классификации разговоров."""

    items: list[ClassificationNode] = Field(default_factory=list)


class ReportClassificationQuery(BaseModel):
    """Вход операции report_classification; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    day_from: date
    day_to: date
    source: CallSource = CallSource.NCC


class ReportCallsQuery(BaseModel):
    """Вход операции report_calls; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    day_from: date
    day_to: date
    operator_id: uuid.UUID | None = None
    operator_node_id: uuid.UUID | None = None
    ai_node_id: uuid.UUID | None = None
    source: CallSource = CallSource.NCC
