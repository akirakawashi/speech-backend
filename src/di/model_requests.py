"""Сборка подготовки модельных заданий по настройкам бэкенда."""

from typing import Annotated

from fastapi import Depends

from src.application.model_requests.service import ModelRequestService
from src.infrastructure.prompts.catalogue import FilePromptCatalogue
from src.setting.config import Settings, get_settings


def build_model_request_service(settings: Settings) -> ModelRequestService:
    """Внедрить каталог, имена моделей, лимит ответа и служебный токен."""
    return ModelRequestService(
        FilePromptCatalogue(),
        model=settings.speech_analysis_llm_model.strip(),
        free_model=settings.speech_analysis_llm_free_model.strip(),
        candidate_model=settings.speech_analysis_llm_candidate_model.strip(),
        max_tokens=settings.speech_analysis_llm_max_tokens,
        worker_token=settings.speech_worker_api_token,
    )


def model_request_application(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ModelRequestService:
    """Собрать сценарий для HTTP-запроса без открытия соединения PostgreSQL."""
    return build_model_request_service(settings)
