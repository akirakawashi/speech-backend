"""Локальный адаптер подготовки для сохранённых фоновых команд бэкенда."""

from src.application.dto.model_execution import ModelPreparationRequest, PreparedModelRequest
from src.application.errors.external import AnalysisError
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.model_requests.service import ModelRequestService


class LocalModelRequestPreparer:
    """Использовать тот же сценарий подготовки без HTTP-вызова к самому себе."""

    def __init__(self, service: ModelRequestService) -> None:
        """Принять сценарий бэкенда, собранный DI."""
        self._service = service

    async def prepare(self, request: ModelPreparationRequest) -> PreparedModelRequest:
        """Подготовить DTO; ожидаемый отказ выразить контрактом клиента анализа."""
        try:
            return await self._service.prepare(request)
        except ApplicationError as exc:
            raise AnalysisError(
                "model_preparation_failed", retryable=exc.kind == ErrorKind.UNAVAILABLE
            ) from None
