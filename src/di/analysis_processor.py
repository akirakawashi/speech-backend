"""Сборка самостоятельного CPU-анализа в event loop без зависимостей аудиоворкера."""

import asyncio

from src.application.analysis.execution import AnalysisExecutionService
from src.application.analysis.interpretation import AnalysisInterpretationService
from src.application.analysis.processing import AnalysisProcessingService
from src.application.dto.analysis_processing import AnalysisProcessorOptions
from src.di.analysis import build_analysis_client
from src.infrastructure.persistence.analysis_processing import PostgresAnalysisProcessingRepository
from src.infrastructure.persistence.session import (
    SPEECH_REQUIRED_SCHEMA_REVISION,
    get_speech_ms_engine,
    get_speech_ms_sessionmaker,
    speech_schema_is_ready,
)
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.infrastructure.runtime.analysis_processor import AnalysisProcessor, install_signal_handlers
from src.infrastructure.runtime.logs import configure_logging
from src.setting.config import Settings, get_settings


async def run_analysis_processor(options: AnalysisProcessorOptions, settings: Settings) -> None:
    """Проверить LLM и схему, собрать порты и закрыть пул при успехе, ошибке или отмене."""
    model = build_analysis_client(settings)
    engine = get_speech_ms_engine(settings)
    try:
        sessions = get_speech_ms_sessionmaker(settings)
        async with sessions() as session:
            if not await speech_schema_is_ready(session):
                raise RuntimeError(
                    "Speech schema is not ready: apply migrations to "
                    + SPEECH_REQUIRED_SCHEMA_REVISION
                )
        application = AnalysisProcessingService(
            PostgresAnalysisProcessingRepository(sessions),
            model,
            AnalysisExecutionService(AnalysisInterpretationService()),
        )
        processor = AnalysisProcessor(
            PostgresJobQueue(sessions, engine=engine), application, lanes=options.lanes
        )
        install_signal_handlers(processor)
        if options.once:
            await processor.run_once()
        else:
            await processor.run_forever()
    finally:
        await engine.dispose()


def run(options: AnalysisProcessorOptions) -> int:
    """Прочитать Settings один раз в DI, выполнить CPU-процесс и вернуть код штатной остановки."""
    configure_logging()
    asyncio.run(run_analysis_processor(options, get_settings()))
    return 0
