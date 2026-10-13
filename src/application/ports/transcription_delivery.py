"""Порты одной транзакции приёма распознавания; внешние вызовы ей не принадлежат."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from src.application.dto.transcription_delivery import (
    ReceivedTranscription,
    ReceivingCall,
    StoredTranscriptionReceipt,
    TranscriptionClaim,
    TranscriptionLease,
)
from src.application.ports.queue import NewJob


class TranscriptionReceptionTransaction(Protocol):
    """Короткие операции под блокировками звонка и заданий без самостоятельного commit."""

    async def lock_call(self, call_id: UUID) -> ReceivingCall | None:
        """Заблокировать звонок; None означает отсутствие записи."""
        ...

    async def lock_jobs(self, call_id: UUID) -> tuple[TranscriptionLease, ...]:
        """Заблокировать задания звонка без ожидания чужой активной транзакции."""
        ...

    async def receipt(self, job_id: UUID) -> StoredTranscriptionReceipt | None:
        """Прочитать квитанцию из метаданных уже заблокированного задания."""
        ...

    async def database_time(self) -> datetime:
        """Вернуть текущее время базы после получения блокировок."""
        ...

    async def analysis_attempts(self) -> int:
        """Прочитать действующий лимит попыток анализа из настроек бэкенда."""
        ...

    async def invalidate_results(self, call_id: UUID) -> None:
        """Снять прежний анализ/оценку и убрать неработающие задания старой оценки."""
        ...

    async def save_transcription(self, call_id: UUID, result: ReceivedTranscription) -> UUID:
        """Заменить расшифровку и вернуть UUID новой записи."""
        ...

    async def enqueue_analysis(self, job: NewJob) -> UUID:
        """Поставить анализ либо сбросить его неработающее прежнее задание."""
        ...

    async def complete(
        self, claim: TranscriptionClaim, receipt: StoredTranscriptionReceipt
    ) -> bool:
        """Повторно проверить аренду, завершить задание и записать квитанцию; False откатывается."""
        ...


class TranscriptionReceptionRepository(Protocol):
    """Создать транзакцию приёма; выход выполняет commit или rollback."""

    def transaction(self) -> AbstractAsyncContextManager[TranscriptionReceptionTransaction]:
        """Вернуть единицу работы; подтверждение отдаётся после успешного выхода."""
        ...
