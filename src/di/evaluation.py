"""Сборка пакетной оценки из PostgreSQL и клиента выбранной модели."""

from src.application.quality.batch import EvaluationService
from src.application.quality.execution import QualityExecutionService
from src.di.worker_tasks import build_quality_preparation
from src.infrastructure.clients.local_quality import LocalQualitySteps
from src.infrastructure.clients.quality import PreparedQualityModelClient, build_quality_client
from src.infrastructure.persistence.evaluation import PostgresEvaluationRepository
from src.infrastructure.persistence.session import get_speech_ms_sessionmaker
from src.setting.config import Settings, get_settings


def build_evaluation() -> EvaluationService:
    """Прочитать настройки при запуске, собрать хранение и отложенную модель оценки."""
    settings = get_settings()
    return EvaluationService(
        PostgresEvaluationRepository(get_speech_ms_sessionmaker(settings)),
        lambda: build_quality_client(settings),
        timezone=settings.speech_display_timezone,
    )


def build_queue_evaluation(settings: Settings) -> QualityExecutionService:
    """Собрать оценку в сохранённой команде бэкенда по общему контракту шагов."""
    return QualityExecutionService(
        LocalQualitySteps(build_quality_preparation(settings), settings.speech_worker_api_token),
        PreparedQualityModelClient(build_quality_client(settings)),
    )
