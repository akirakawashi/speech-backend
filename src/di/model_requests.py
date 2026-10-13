"""Сборка подготовки модельных заданий по настройкам бэкенда."""

from src.application.model_requests.service import ModelRequestService
from src.infrastructure.prompts.catalogue import FilePromptCatalogue
from src.setting.config import Settings


def build_model_request_service(settings: Settings) -> ModelRequestService:
    """Внедрить каталог, имена моделей и лимит ответа без служебного HTTP."""
    return ModelRequestService(
        FilePromptCatalogue(),
        model=settings.speech_analysis_llm_model.strip(),
        free_model=settings.speech_analysis_llm_free_model.strip(),
        candidate_model=settings.speech_analysis_llm_candidate_model.strip(),
        max_tokens=settings.speech_analysis_llm_max_tokens,
    )
