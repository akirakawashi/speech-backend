"""Технические операции очереди в сессии вызывающего адаптера PostgreSQL."""

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.application.ports.queue import NewJob


class TransactionalQueue(Protocol):
    """Передать enqueue, reprocess и complete одной SQL-транзакции адаптера."""

    async def enqueue(self, session: AsyncSession, job: NewJob) -> uuid.UUID | None:
        """Поставить задание. `None`, если такое уже есть.

        Идемпотентность обеспечивает уникальность `(call_id, kind)`: у звонка не
        бывает двух заданий одного этапа. Повторная постановка — не ошибка, это
        обычный исход повторного прогона за ту же дату.
        """
        ...

    async def reprocess(
        self, session: AsyncSession, job: NewJob, *, retry_job_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        """Restart a terminal pipeline in the caller's transaction; never commit.

        Lock the call and its existing jobs before checking *all* stages for
        queued/running work. Reset the selected terminal job in place (or insert
        it), and remove only terminal downstream jobs to allow normal enqueue.
        A retry additionally requires the exact job ID to remain failed/dead.
        The caller resets results and writes the audit in this same transaction,
        and must roll back on failure, including lock conflicts.
        """
        ...

    async def complete(self, session: AsyncSession, job_id: uuid.UUID, worker_id: str) -> bool:
        """Завершить успешно, в транзакции вызывающего.

        `False` означает, что задание больше не принадлежит этому воркеру — его
        аренда истекла и его переиграл кто-то другой. Вызывающий обязан
        откатить транзакцию: иначе в базу попадёт результат «зомби», уже
        переписанный чужой попыткой.
        """
        ...
