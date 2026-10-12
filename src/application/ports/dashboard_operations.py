"""Порт операций dashboard без технических типов реализации."""

from typing import Protocol

from src.application.dto.dashboard_operations import (
    DashboardFileQuery,
    DashboardQuery,
    DashboardResponse,
)
from src.application.dto.files import FileResult


class DashboardRepository(Protocol):
    """Внешние операции, необходимые сценариям dashboard."""

    async def dashboard(self, query: DashboardQuery) -> DashboardResponse:
        """Выполнить операцию dashboard и вернуть её типизированную проекцию."""
        ...

    async def dashboard_file(self, query: DashboardFileQuery) -> FileResult:
        """Выполнить операцию dashboard_file и вернуть её типизированную проекцию."""
        ...
