"""Порт прямого вызова модели разбора разговора."""

from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from src.application.dto.model_execution import ModelExecutionResult


@runtime_checkable
class AnalysisBackend(Protocol):
    """Вызов модели разбора с типизированным JSON, независимо от транспорта."""

    async def run(self, inputs: dict[str, JsonValue]) -> ModelExecutionResult:
        """Вызвать LLM и вернуть ответ с фактами именно этого запроса.

        Args:
            inputs: Именованные данные выбранного сценария.

        Returns:
            Непроверенный ответ и метаданные реально отправленного запроса.

        Raises:
            AnalysisError: При отказе подготовки, транспорта или модели.
        """
        ...
