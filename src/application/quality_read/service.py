"""Сценарии quality_read: проверка доступа и вызов внедрённых операций."""

from __future__ import annotations

from src.application.authorization import (
    PERM_EXPORT,
    PERM_QUALITY,
    ensure_permission,
)
from src.application.calls.period import normalized_period
from src.application.dto.files import FileResult
from src.application.dto.quality import CURRENT_SCRIPT
from src.application.dto.quality_read_operations import (
    CallQualityQuery,
    CallQualityResponse,
    QualityCallsQuery,
    QualityCallsResponse,
    QualityReviewFileQuery,
    QualitySummaryQuery,
    QualitySummaryResponse,
)
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.quality_read_operations import QualityReadRepository


class QualityReadService:
    """Выполнять сценарии quality_read через заменяемый порт."""

    def __init__(self, repository: QualityReadRepository) -> None:
        """Принять реализацию внешних операций; не создавать ресурсы самостоятельно."""
        self._repository = repository

    async def call_quality(self, query: CallQualityQuery) -> CallQualityResponse:
        """По UUID вернуть действующую оценку звонка; нужно право quality."""
        ensure_permission(query.actor, PERM_QUALITY)
        return await self._repository.call_quality(query)

    async def quality_summary(self, query: QualitySummaryQuery) -> QualitySummaryResponse:
        """За охват DTO вернуть сводку этапов и сотрудников; нужно право quality."""
        ensure_permission(query.actor, PERM_QUALITY)
        return await self._repository.quality_summary(normalized_period(query))

    async def quality_calls(self, query: QualityCallsQuery) -> QualityCallsResponse:
        """Проверить фильтры скрипта и вернуть страницу звонков; нужно право quality."""
        ensure_permission(query.actor, PERM_QUALITY)
        if query.stage is not None and CURRENT_SCRIPT.stage(query.stage) is None:
            raise ApplicationError(ErrorKind.INVALID_INPUT, "unknown_stage")
        if query.outcome is not None and query.outcome not in dict(CURRENT_SCRIPT.outcomes):
            raise ApplicationError(ErrorKind.INVALID_INPUT, "unknown_outcome")
        return await self._repository.quality_calls(normalized_period(query))

    async def quality_review_file(self, query: QualityReviewFileQuery) -> FileResult:
        """По охвату DTO вернуть файл сверки; нужны права quality и export."""
        ensure_permission(query.actor, PERM_QUALITY)
        ensure_permission(query.actor, PERM_EXPORT)
        return await self._repository.quality_review_file(normalized_period(query))
