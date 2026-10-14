"""Сборка служебных сценариев без выполнения HTTP-запросов к моделям."""

from src.application.quality.preparation import QualityPreparationService
from src.domain.calls.schedule import CollectionSchedule
from src.infrastructure.persistence.evaluation import PostgresEvaluationRepository
from src.infrastructure.persistence.session import get_speech_ms_sessionmaker
from src.setting.config import Settings


def build_quality_preparation(settings: Settings) -> QualityPreparationService:
    """Внедрить хранение, имя модели и лимит ответа из настроек бэкенда."""
    return QualityPreparationService(
        PostgresEvaluationRepository(get_speech_ms_sessionmaker(settings)),
        model=settings.speech_quality_llm_model.strip(),
        max_tokens=settings.speech_quality_llm_max_tokens,
    )


def build_worker_schedule(settings: Settings) -> CollectionSchedule:
    """Проверить часовой пояс, интервалы и ночное окно до обслуживания запросов."""
    return CollectionSchedule(
        timezone=settings.speech_display_timezone,
        ncc_interval_seconds=settings.speech_ncc_ingest_interval_seconds,
        telphin_interval_seconds=settings.speech_telphin_ingest_interval_seconds,
        quality_interval_seconds=settings.speech_quality_interval_seconds,
        immediate_share=settings.speech_ncc_immediate_share,
        night_from=settings.speech_night_from,
        night_until=settings.speech_night_until,
        quality_limit=settings.speech_quality_batch_limit,
    )
