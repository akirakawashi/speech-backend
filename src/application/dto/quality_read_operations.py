"""Типизированные команды и проекции операций quality_read."""

from __future__ import annotations

import uuid
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from src.application.authorization import Principal
from src.application.dto.api import (
    CallQualityResponse as CallQualityResponse,
    QualityCallsResponse as QualityCallsResponse,
    QualitySummaryResponse as QualitySummaryResponse,
)
from src.application.dto.period import PeriodQuery
from src.application.dto.review import REVIEW_SIZE

EvaluationStatus = Literal["evaluated", "failed", "pending", "unavailable"]

Bucket = Literal["good", "fair", "poor"]

StageState = Literal["failed", "done", "skipped"]

CallSort = Literal["newest", "worst", "best"]

CALLS_PAGE_SIZE: Final[int] = 50

CALLS_PAGE_MAX: Final[int] = 200


class CallQualityQuery(BaseModel):
    """Вход операции call_quality; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    call_id: uuid.UUID
    actor: Principal


class QualitySummaryQuery(PeriodQuery):
    """Вход операции quality_summary; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    operator_id: uuid.UUID | None = None
    unassigned: bool = False


class QualityCallsQuery(PeriodQuery):
    """Вход операции quality_calls; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    operator_id: uuid.UUID | None = None
    unassigned: bool = False
    applies: bool | None = None
    evaluation_status: EvaluationStatus | None = None
    bucket: Bucket | None = None
    stage: str | None = None
    stage_state: StageState = "failed"
    outcome: str | None = None
    violations: bool = False
    sort: CallSort = "newest"
    limit: int = CALLS_PAGE_SIZE
    offset: int = 0


class QualityReviewFileQuery(PeriodQuery):
    """Вход операции quality_review_file; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    operator_id: uuid.UUID | None = None
    unassigned: bool = False
    limit: int = REVIEW_SIZE
