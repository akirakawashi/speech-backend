"""Подготовка шагов внутри сохранённой команды бэкенда для совместимости."""

from src.application.dto.quality_execution import QualityStep, QualityStepRequest
from src.application.quality.preparation import QualityPreparationService


class LocalQualitySteps:
    """Использовать сценарий бэкенда без HTTP-вызова собственного API."""

    def __init__(self, service: QualityPreparationService, token: str) -> None:
        """Принять собранный сценарий и настроенный служебный ключ."""
        self._service = service
        self._token = token

    async def prepare(self, command: QualityStepRequest) -> QualityStep:
        """Подготовить шаг по тому же контракту, что используется отдельным воркером."""
        return await self._service.prepare_authorized(command, f"Bearer {self._token}")
