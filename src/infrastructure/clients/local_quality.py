"""Подготовка шагов внутри сохранённой команды бэкенда для совместимости."""

from src.application.dto.quality_execution import QualityStep, QualityStepRequest
from src.application.quality.preparation import QualityPreparationService


class LocalQualitySteps:
    """Использовать сценарий бэкенда без HTTP-вызова собственного API."""

    def __init__(self, service: QualityPreparationService) -> None:
        """Принять локальный сценарий; служебный ключ для вычисления не нужен."""
        self._service = service

    async def prepare(self, command: QualityStepRequest) -> QualityStep:
        """Продолжить локальную оценку по сохранённым ответам без служебного HTTP."""
        return await self._service.prepare(command)
