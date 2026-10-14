"""Сборка пакетной оценки из PostgreSQL и клиента выбранной модели."""

from src.application.dto.maintenance import QualityQueueReport
from src.application.dto.quality_batch import RunEvaluation
from src.application.quality.batch import EvaluationService
from src.application.quality.queue_command import QueuedEvaluationService
from src.application.quality.scheduling import WorkerMaintenanceService
from src.di.worker_tasks import build_worker_schedule
from src.infrastructure.clients.quality import build_quality_client
from src.infrastructure.persistence.evaluation import PostgresEvaluationRepository
from src.infrastructure.persistence.quality_jobs import PostgresQualityQueueRepository
from src.infrastructure.persistence.session import (
    SPEECH_REQUIRED_SCHEMA_REVISION,
    get_speech_ms_engine,
    get_speech_ms_sessionmaker,
    speech_schema_is_ready,
)
from src.setting.config import get_settings


def build_evaluation() -> EvaluationService:
    """Прочитать настройки при запуске, собрать хранение и отложенную модель оценки."""
    settings = get_settings()
    return EvaluationService(
        PostgresEvaluationRepository(get_speech_ms_sessionmaker(settings)),
        lambda: build_quality_client(settings),
        timezone=settings.speech_display_timezone,
    )


async def run_queued_evaluation(query: RunEvaluation) -> QualityQueueReport:
    """Поставить оценку локально без модели/токена; проверить схему и закрыть пул."""
    settings = get_settings()
    engine = get_speech_ms_engine(settings)
    try:
        sessions = get_speech_ms_sessionmaker(settings)
        async with sessions() as session:
            if not await speech_schema_is_ready(session):
                raise RuntimeError(
                    "Speech schema is not ready: apply migrations to "
                    + SPEECH_REQUIRED_SCHEMA_REVISION
                )
        application = WorkerMaintenanceService(
            build_worker_schedule(settings),
            PostgresQualityQueueRepository(sessions),
            PostgresEvaluationRepository(sessions),
        )
        return await QueuedEvaluationService(application).run(query)
    finally:
        await engine.dispose()
