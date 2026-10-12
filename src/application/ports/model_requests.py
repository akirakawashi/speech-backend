"""Порты каталога промптов и подготовки запроса независимо от транспорта."""

from typing import Protocol, runtime_checkable

from src.application.dto.model_execution import (
    ModelPreparationRequest,
    ModelScenario,
    PreparedModelRequest,
    PromptTemplate,
)


class PromptCatalogue(Protocol):
    """Получить версионированный шаблон по назначению обработки."""

    def get(self, scenario: ModelScenario) -> PromptTemplate:
        """Вернуть шаблон; отсутствие сценария означает ошибку развёртывания."""
        ...


@runtime_checkable
class ModelRequestPreparer(Protocol):
    """Подготовить запрос к LLM через локальный сценарий или HTTP-контракт."""

    async def prepare(self, request: ModelPreparationRequest) -> PreparedModelRequest:
        """Принять входной DTO и вернуть сообщения и параметры модели."""
        ...
