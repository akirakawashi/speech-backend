"""Сборка прямого вызова LLM и подготовки запроса без глобальных клиентов."""

from src.application.dto.model_execution import ModelProfile, ModelScenario
from src.application.errors.external import AnalysisNotConfigured
from src.di.model_requests import build_model_request_service
from src.infrastructure.clients.analysis import SpeechAnalysisClient
from src.infrastructure.prompts.preparer import LocalModelRequestPreparer
from src.setting.config import Settings


def build_analysis_client(
    settings: Settings,
    *,
    scenario: ModelScenario = "analysis",
    profile: ModelProfile = "production",
) -> SpeechAnalysisClient:
    """Проверить настройки и собрать исполнителя указанного модельного сценария."""
    base_url = settings.speech_analysis_llm_base_url.strip().rstrip("/")
    if not base_url:
        raise AnalysisNotConfigured("SPEECH_ANALYSIS_LLM_BASE_URL is required")
    if not settings.speech_analysis_llm_model.strip():
        raise AnalysisNotConfigured("SPEECH_ANALYSIS_LLM_MODEL is required")
    preparer = LocalModelRequestPreparer(build_model_request_service(settings))
    return SpeechAnalysisClient(
        base_url=base_url,
        api_key=settings.speech_analysis_llm_api_key,
        timeout_seconds=settings.speech_analysis_llm_timeout_seconds,
        preparer=preparer,
        scenario=scenario,
        profile=profile,
    )
