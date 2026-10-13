"""Ожидаемая конкуренция при короткой транзакции анализа."""

from src.application.errors.job_processing import (
    JobProcessingBusy as AnalysisProcessingBusy,
    LostJobProcessingLease as LostAnalysisLease,
)

__all__ = ["AnalysisProcessingBusy", "LostAnalysisLease"]
