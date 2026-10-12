"""Типизированные команды и проекции операций calls."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from src.application.authorization import Principal
from src.application.dto.api import (
    CallResponse as CallResponse,
    ClassifierResponse as ClassifierResponse,
    MeResponse as MeResponse,
    OptionsResponse as OptionsResponse,
    SearchResponse as SearchResponse,
    StatsResponse as StatsResponse,
)
from src.application.dto.calls import (
    CallSource,
    Product,
    Segment,
)
from src.application.dto.period import PeriodQuery
from src.application.dto.search import PAGE_SIZE


class MeQuery(BaseModel):
    """Вход операции me; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal


class SearchQuery(PeriodQuery):
    """Вход операции search; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    q: str = ""
    operator_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    segment: Segment | None = None
    product: Product | None = None
    source: CallSource = CallSource.NCC
    page: int = 1
    size: int = PAGE_SIZE


class StatsQuery(PeriodQuery):
    """Вход операции stats; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    source: CallSource = CallSource.NCC


class CallCardQuery(BaseModel):
    """Вход операции call_card; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    call_id: uuid.UUID
    actor: Principal
    q: str = ""


class OperatorsQuery(BaseModel):
    """Вход операции operators; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    source: CallSource = CallSource.NCC


class ProjectsQuery(BaseModel):
    """Вход операции projects; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    source: CallSource = CallSource.NCC


class ClassifierQuery(BaseModel):
    """Вход операции classifier; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
