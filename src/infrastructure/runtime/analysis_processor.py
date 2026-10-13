"""Сохранённый запуск только analyze поверх общего CPU-цикла."""

from collections.abc import Sequence

from src.application.analysis.processing import AnalysisProcessingService
from src.application.dto.calls import JobKind, JobLane
from src.application.ports.queue import JobQueue
from src.infrastructure.runtime.job_processor import (
    JobProcessor,
    install_signal_handlers as install_signal_handlers,
)


class AnalysisProcessor(JobProcessor):
    """Исполнить только analyze, сохраняя существующие параметры запуска."""

    def __init__(
        self,
        queue: JobQueue,
        application: AnalysisProcessingService,
        *,
        lanes: Sequence[JobLane],
        worker_id: str | None = None,
        idle_seconds: float = 2,
        heartbeat_seconds: float = 30,
        reaper_seconds: float = 60,
    ) -> None:
        """Внедрить анализ и его восстановление; другие этапы не захватываются."""
        super().__init__(
            queue,
            {JobKind.ANALYZE: application},
            lanes=lanes,
            recoveries={JobKind.ANALYZE: application},
            worker_id=worker_id,
            idle_seconds=idle_seconds,
            heartbeat_seconds=heartbeat_seconds,
            reaper_seconds=reaper_seconds,
        )
