"""Независимая постановка оценки бэкендом по существующему расписанию."""

from datetime import UTC, datetime

from src.application.dto.maintenance import QualityQueueRequest
from src.application.dto.quality_batch import RunEvaluation
from src.application.ports.job_processing import EvaluationSchedulingApplication
from src.domain.calls.schedule import CollectionSchedule


class EvaluationProcessorSchedule:
    """Ставить готовые звонки Телфина порциями, сохраняя дедупликацию и skip_failed."""

    def __init__(
        self, maintenance: EvaluationSchedulingApplication, policy: CollectionSchedule
    ) -> None:
        """Внедрить локальный сценарий очереди и неизменную политику; модель не вызывается."""
        self._maintenance = maintenance
        self._policy = policy
        self._last_started: datetime | None = None

    async def tick(self, *, now: datetime | None = None) -> None:
        """При наступлении срока поставить порцию; после сбоя сохранить возможность повтора.

        На старте выполняется один догон. Время предыдущего начала обновляется
        после успешного commit постановки. Повторные CPU-процессы не создают
        дубликаты благодаря общей PostgreSQL-очереди.
        """
        instant = now or datetime.now(UTC)
        if self._policy.next_run("quality", instant, self._last_started) > instant:
            return
        await self._maintenance.enqueue(
            QualityQueueRequest(
                selection=RunEvaluation(
                    everything=True,
                    limit=self._policy.quality_limit,
                    skip_failed=True,
                    apply=True,
                )
            )
        )
        self._last_started = instant
