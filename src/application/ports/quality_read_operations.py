"""Порт операций quality_read без технических типов реализации."""

from typing import Protocol

from src.application.dto.files import FileResult
from src.application.dto.quality_read_operations import (
    CallQualityQuery,
    CallQualityResponse,
    QualityCallsQuery,
    QualityCallsResponse,
    QualityReviewFileQuery,
    QualitySummaryQuery,
    QualitySummaryResponse,
)


class QualityReadRepository(Protocol):
    """Внешние операции, необходимые сценариям quality_read."""

    async def call_quality(self, query: CallQualityQuery) -> CallQualityResponse:
        """Выполнить операцию call_quality и вернуть её типизированную проекцию."""
        ...

    async def quality_summary(self, query: QualitySummaryQuery) -> QualitySummaryResponse:
        """Выполнить операцию quality_summary и вернуть её типизированную проекцию."""
        ...

    async def quality_calls(self, query: QualityCallsQuery) -> QualityCallsResponse:
        """Выполнить операцию quality_calls и вернуть её типизированную проекцию."""
        ...

    async def quality_review_file(self, query: QualityReviewFileQuery) -> FileResult:
        """Выполнить операцию quality_review_file и вернуть её типизированную проекцию."""
        ...
