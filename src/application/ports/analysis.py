"""Порт прямого вызова модели разбора разговора."""

from typing import Protocol, runtime_checkable

from pydantic import JsonValue


@runtime_checkable
class AnalysisBackend(Protocol):
    """Вызов модели разбора с типизированным JSON, независимо от транспорта."""

    async def run(self, inputs: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Передать вход модели и вернуть её выход для проверки сценарием."""
        ...
