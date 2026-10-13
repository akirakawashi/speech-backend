"""Сборка единого CPU-процесса бэкенда с локальными анализом и оценкой."""

import asyncio

from src.application.analysis.execution import AnalysisExecutionService
from src.application.analysis.processing import AnalysisProcessingService
from src.application.dto.calls import JobKind
from src.application.dto.job_processing import SpeechProcessorOptions
from src.application.errors.quality import QualityModelNotConfigured
from src.application.ports.job_processing import (
    JobProcessingApplication,
    JobProcessingRecovery,
    JobProcessingSchedule,
)
from src.application.quality.processing import EvaluationProcessingService
from src.application.quality.processor_schedule import EvaluationProcessorSchedule
from src.application.quality.scheduling import WorkerMaintenanceService
from src.di.analysis import build_analysis_client
from src.di.worker_tasks import build_worker_schedule
from src.infrastructure.clients.quality import build_quality_client
from src.infrastructure.persistence.analysis_processing import PostgresAnalysisProcessingRepository
from src.infrastructure.persistence.evaluation import PostgresEvaluationRepository
from src.infrastructure.persistence.evaluation_processing import (
    PostgresEvaluationProcessingRepository,
)
from src.infrastructure.persistence.quality_jobs import PostgresQualityQueueRepository
from src.infrastructure.persistence.session import (
    SPEECH_REQUIRED_SCHEMA_REVISION,
    get_speech_ms_engine,
    get_speech_ms_sessionmaker,
    speech_schema_is_ready,
)
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.infrastructure.runtime.job_processor import JobProcessor, install_signal_handlers
from src.infrastructure.runtime.logs import configure_logging
from src.setting.config import Settings, get_settings


async def run_speech_processor(options: SpeechProcessorOptions, settings: Settings) -> None:
    """Проверить выбранные модели/схему, выполнить последовательный процесс и закрыть пул."""
    analysis = build_analysis_client(settings) if JobKind.ANALYZE in options.stages else None
    quality = build_quality_client(settings) if JobKind.EVALUATE in options.stages else None
    if quality is not None and not quality.model.strip():
        raise QualityModelNotConfigured("SPEECH_QUALITY_LLM_MODEL is required")
    engine = get_speech_ms_engine(settings)
    try:
        sessions = get_speech_ms_sessionmaker(settings)
        async with sessions() as session:
            if not await speech_schema_is_ready(session):
                raise RuntimeError(
                    "Speech schema is not ready: apply migrations to "
                    + SPEECH_REQUIRED_SCHEMA_REVISION
                )
        applications: dict[JobKind, JobProcessingApplication] = {}
        recoveries: dict[JobKind, JobProcessingRecovery] = {}
        schedule: JobProcessingSchedule | None = None
        if analysis is not None:
            application = AnalysisProcessingService(
                PostgresAnalysisProcessingRepository(sessions), analysis, AnalysisExecutionService()
            )
            applications[JobKind.ANALYZE] = application
            recoveries[JobKind.ANALYZE] = application
        if quality is not None:
            applications[JobKind.EVALUATE] = EvaluationProcessingService(
                PostgresEvaluationProcessingRepository(sessions),
                quality,
            )
            if options.schedule_evaluations:
                policy = build_worker_schedule(settings)
                schedule = EvaluationProcessorSchedule(
                    WorkerMaintenanceService(
                        policy,
                        PostgresQualityQueueRepository(sessions),
                        PostgresEvaluationRepository(sessions),
                        worker_token="",
                    ),
                    policy,
                )
        processor = JobProcessor(
            PostgresJobQueue(sessions, engine=engine),
            applications,
            lanes=options.lanes,
            recoveries=recoveries,
            schedule=schedule,
        )
        install_signal_handlers(processor)
        if options.once:
            await processor.run_once()
        else:
            await processor.run_forever()
    finally:
        await engine.dispose()


def run(options: SpeechProcessorOptions) -> int:
    """Прочитать Settings один раз, запустить CPU-процесс и вернуть код штатной остановки."""
    configure_logging()
    asyncio.run(run_speech_processor(options, get_settings()))
    return 0
