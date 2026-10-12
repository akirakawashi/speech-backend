"""Сценарии reports: проверка доступа и вызов внедрённых операций."""

from __future__ import annotations

from src.application.authorization import (
    PERM_ADMIN,
    ensure_permission,
)
from src.application.dto.files import FileResult
from src.application.dto.reports_operations import (
    ClassificationResponse,
    ReportCallsQuery,
    ReportClassificationQuery,
)
from src.application.ports.reports_operations import ReportsRepository


class ReportsService:
    """Выполнять сценарии reports через заменяемый порт."""

    def __init__(self, repository: ReportsRepository) -> None:
        """Принять реализацию внешних операций; не создавать ресурсы самостоятельно."""
        self._repository = repository

    async def report_classification(
        self, query: ReportClassificationQuery
    ) -> ClassificationResponse:
        """За календарный период DTO вернуть узлы для отчёта; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.report_classification(query)

    async def report_calls(self, query: ReportCallsQuery) -> FileResult:
        """По периоду и фильтрам DTO вернуть файл звонков; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.report_calls(query)
