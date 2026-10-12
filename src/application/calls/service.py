"""Сценарии calls: проверка доступа и вызов внедрённых операций."""

from __future__ import annotations

from src.application.authorization import (
    PERM_SEARCH,
    ensure_permission,
)
from src.application.calls.period import normalized_period
from src.application.dto.calls_operations import (
    CallCardQuery,
    CallResponse,
    ClassifierQuery,
    ClassifierResponse,
    MeQuery,
    MeResponse,
    OperatorsQuery,
    OptionsResponse,
    ProjectsQuery,
    SearchQuery,
    SearchResponse,
    StatsQuery,
    StatsResponse,
)
from src.application.ports.calls_operations import CallsRepository


class CallsService:
    """Выполнять сценарии calls через заменяемый порт."""

    def __init__(self, repository: CallsRepository) -> None:
        """Принять реализацию внешних операций; не создавать ресурсы самостоятельно."""
        self._repository = repository

    async def me(self, query: MeQuery) -> MeResponse:
        """Сохранить факт входа и вернуть личность пользователя с разрешениями."""
        return await self._repository.me(query)

    async def search(self, query: SearchQuery) -> SearchResponse:
        """По фильтрам и тексту запроса вернуть страницу звонков; нужно право search."""
        ensure_permission(query.actor, PERM_SEARCH)
        return await self._repository.search(normalized_period(query))

    async def stats(self, query: StatsQuery) -> StatsResponse:
        """За выбранный период вернуть счётчики обработки и частые темы; нужно право search."""
        ensure_permission(query.actor, PERM_SEARCH)
        return await self._repository.stats(normalized_period(query))

    async def call_card(self, query: CallCardQuery) -> CallResponse:
        """По UUID вернуть карточку звонка и реплики; нужно право search."""
        ensure_permission(query.actor, PERM_SEARCH)
        return await self._repository.call_card(query)

    async def operators(self, query: OperatorsQuery) -> OptionsResponse:
        """Вернуть доступных операторов выбранного источника; нужно право search."""
        ensure_permission(query.actor, PERM_SEARCH)
        return await self._repository.operators(query)

    async def projects(self, query: ProjectsQuery) -> OptionsResponse:
        """Вернуть проекты со звонками выбранного источника; нужно право search."""
        ensure_permission(query.actor, PERM_SEARCH)
        return await self._repository.projects(query)

    async def classifier(self, query: ClassifierQuery) -> ClassifierResponse:
        """Вернуть дерево для фильтрации звонков; нужно право search."""
        ensure_permission(query.actor, PERM_SEARCH)
        return await self._repository.classifier(query)
