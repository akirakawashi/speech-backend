"""Конкуренция при короткой записи результата CPU-задания."""

from src.domain.calls.job_processing import LostJobProcessingLease as LostJobProcessingLease


class JobProcessingBusy(RuntimeError):
    """Изменившийся вход или занятая блокировка требуют отсрочки без расхода попытки."""
