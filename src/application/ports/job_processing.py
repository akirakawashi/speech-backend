"""Порты исполнения, восстановления и независимой постановки CPU-заданий."""

from collections.abc import Sequence
from typing import Protocol

from src.application.dto.calls import JobLane
from src.application.dto.job_processing import JobProcessingClaim, JobProcessingOutcome
from src.application.dto.maintenance import QualityQueueReport, QualityQueueRequest
from src.application.ports.queue import ExpiredJob


class JobProcessingApplication(Protocol):
    """Выполнить один захват без знания цикла очереди."""

    async def process(self, claim: JobProcessingClaim) -> JobProcessingOutcome:
        """Вернуть исход после атомарной записи; отмена/ошибка базы допускает восстановление."""
        ...


class JobProcessingRecovery(Protocol):
    """Восстановить дополнительные бизнес-состояния после просроченных заданий."""

    async def mark_expired(self, expired: ExpiredJob) -> None:
        """Проверить актуальность и спроецировать окончательный отказ."""
        ...

    async def repair_expired_failures(self, lanes: Sequence[JobLane]) -> None:
        """Повторить прерванную проекцию отказов выбранных полос."""
        ...


class JobProcessingSchedule(Protocol):
    """Поставить независимые задания, если наступил срок приложения."""

    async def tick(self) -> None:
        """Вычислить необходимость постановки и при необходимости вызвать порт очереди."""
        ...


class EvaluationSchedulingApplication(Protocol):
    """Локально применить правила выбора и постановки оценки."""

    async def enqueue(self, command: QualityQueueRequest) -> QualityQueueReport:
        """Поставить готовый Телфин без служебного HTTP; вернуть отчёт после commit."""
        ...
