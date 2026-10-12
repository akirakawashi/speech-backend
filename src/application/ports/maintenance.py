"""Порт атомарного отбора и постановки оценок в очередь."""

from typing import Protocol

from src.application.dto.maintenance import QualityQueueReport
from src.application.dto.quality_batch import CandidateSelection


class QualityQueueRepository(Protocol):
    """Выбрать готовые звонки без активной оценки и дедуплицировать их задания."""

    async def schedule(
        self, selection: CandidateSelection, *, call_id: str | None, apply: bool
    ) -> QualityQueueReport:
        """Проверить охват либо поставить задания одной короткой транзакцией."""
        ...
