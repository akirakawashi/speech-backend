"""Ожидаемая конкуренция при короткой транзакции завершения анализа."""

from src.domain.calls.analysis_processing import LostAnalysisLease as LostAnalysisLease


class AnalysisProcessingBusy(RuntimeError):
    """Снимок изменился или данные заняты; новая попытка должна прочитать свежий вход."""
