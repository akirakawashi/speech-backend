"""Сценарии dashboard: проверка доступа и вызов внедрённых операций."""

from __future__ import annotations

from src.application.authorization import (
    PERM_EXPORT,
    PERM_SEARCH,
    ensure_permission,
)
from src.application.calls.period import normalized_period
from src.application.dto.dashboard_operations import (
    DashboardFileQuery,
    DashboardQuery,
    DashboardResponse,
)
from src.application.dto.files import FileResult
from src.application.ports.dashboard_operations import DashboardRepository


class DashboardService:
    """Выполнять сценарии dashboard через заменяемый порт."""

    def __init__(self, repository: DashboardRepository) -> None:
        """Принять реализацию внешних операций; не создавать ресурсы самостоятельно."""
        self._repository = repository

    async def dashboard(self, query: DashboardQuery) -> DashboardResponse:
        """По охвату DTO вернуть темы и динамику обращений NCC; нужно право search."""
        ensure_permission(query.actor, PERM_SEARCH)
        return await self._repository.dashboard(normalized_period(query))

    async def dashboard_file(self, query: DashboardFileQuery) -> FileResult:
        """По охвату DTO вернуть файл дашборда; нужны права search и export."""
        ensure_permission(query.actor, PERM_SEARCH)
        ensure_permission(query.actor, PERM_EXPORT)
        return await self._repository.dashboard_file(normalized_period(query))
