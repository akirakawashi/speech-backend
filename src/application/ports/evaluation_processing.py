"""Хранение входа оценки и атомарное подтверждение задания без типов PostgreSQL."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from src.application.dto.calls import JobErrorClass, JobStatus
from src.application.dto.evaluation_processing import EvaluationInput, EvaluationSnapshot
from src.application.dto.job_processing import JobProcessingClaim, JobProcessingLease
from src.application.dto.quality_execution import QualityWork


class EvaluationProcessingTransaction(Protocol):
    """Короткая транзакция проверки и записи оценки вместе с исходом задания."""

    async def lock(self, job_id: UUID, call_id: UUID) -> JobProcessingLease | None:
        """Заблокировать звонок/задания и вернуть действующее состояние захвата."""
        ...

    async def database_time(self) -> datetime:
        """Вернуть фактическое время базы после получения блокировок."""
        ...

    async def snapshot(self, call_id: UUID) -> EvaluationInput | None:
        """Заблокировать транскрипт/реплики и прочитать актуальный вход до commit."""
        ...

    async def newer_success(self, call_id: UUID, script_version: str, read_at: datetime) -> bool:
        """Проверить под блокировкой успешную оценку, прочитавшую вход позднее этой попытки."""
        ...

    async def save(self, claim: JobProcessingClaim, work: QualityWork) -> None:
        """Записать роли и оценку/отказ по защищённому входу; commit отсутствует."""
        ...

    async def complete(self, claim: JobProcessingClaim) -> bool:
        """Повторно проверить живой nonce и завершить задание, не меняя статус звонка."""
        ...

    async def fail(
        self, claim: JobProcessingClaim, error_class: JobErrorClass, *, terminal: bool
    ) -> JobStatus | None:
        """Записать backoff/терминальный отказ; None требует rollback из-за потери захвата."""
        ...


class EvaluationProcessingRepository(Protocol):
    """Согласованный снимок до LLM и заменяемая атомарная транзакция после него."""

    async def load(self, claim: JobProcessingClaim) -> EvaluationSnapshot | None:
        """Проверить живой evaluate, прочитать вход и закрыть соединение до модели."""
        ...

    def transaction(self) -> AbstractAsyncContextManager[EvaluationProcessingTransaction]:
        """Открыть UoW, чей успешный выход означает commit."""
        ...

    async def defer(self, claim: JobProcessingClaim) -> bool:
        """Отложить только эту живую попытку без расхода бюджета; False при потере nonce."""
        ...
