"""Порт операций calls без технических типов реализации."""

from typing import Protocol

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


class CallsRepository(Protocol):
    """Внешние операции, необходимые сценариям calls."""

    async def me(self, query: MeQuery) -> MeResponse:
        """Выполнить операцию me и вернуть её типизированную проекцию."""
        ...

    async def search(self, query: SearchQuery) -> SearchResponse:
        """Выполнить операцию search и вернуть её типизированную проекцию."""
        ...

    async def stats(self, query: StatsQuery) -> StatsResponse:
        """Выполнить операцию stats и вернуть её типизированную проекцию."""
        ...

    async def call_card(self, query: CallCardQuery) -> CallResponse:
        """Выполнить операцию call_card и вернуть её типизированную проекцию."""
        ...

    async def operators(self, query: OperatorsQuery) -> OptionsResponse:
        """Выполнить операцию operators и вернуть её типизированную проекцию."""
        ...

    async def projects(self, query: ProjectsQuery) -> OptionsResponse:
        """Выполнить операцию projects и вернуть её типизированную проекцию."""
        ...

    async def classifier(self, query: ClassifierQuery) -> ClassifierResponse:
        """Выполнить операцию classifier и вернуть её типизированную проекцию."""
        ...
