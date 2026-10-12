"""Порт операций reports без технических типов реализации."""

from typing import Protocol

from src.application.dto.files import FileResult
from src.application.dto.reports_operations import (
    ClassificationResponse,
    ReportCallsQuery,
    ReportClassificationQuery,
)


class ReportsRepository(Protocol):
    """Внешние операции, необходимые сценариям reports."""

    async def report_classification(
        self, query: ReportClassificationQuery
    ) -> ClassificationResponse:
        """Выполнить операцию report_classification и вернуть её типизированную проекцию."""
        ...

    async def report_calls(self, query: ReportCallsQuery) -> FileResult:
        """Выполнить операцию report_calls и вернуть её типизированную проекцию."""
        ...
