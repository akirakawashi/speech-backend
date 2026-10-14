"""Порты подготовки и выполнения оценки; воркер не определяет её правила."""

from typing import Protocol

from src.application.dto.quality_execution import (
    QualityModelRequest,
    QualityStep,
    QualityStepRequest,
)


class QualitySteps(Protocol):
    """Получить следующий шаг из сценария бэкенда."""

    async def prepare(self, command: QualityStepRequest) -> QualityStep:
        """Вернуть готовый запрос либо итог по исходному снимку и полученным ответам."""
        ...


class QualityCompletion(Protocol):
    """Выполнить готовый запрос, сохранив выбранные бэкендом параметры."""

    async def complete(self, request: QualityModelRequest) -> str:
        """Вернуть исходный ответ модели либо нормализованный QualityModelError."""
        ...
