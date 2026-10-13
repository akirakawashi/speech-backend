"""Правило захвата анализа поверх общего правила очереди."""

from datetime import datetime

from src.domain.calls.contracts import JobKind
from src.domain.calls.job_processing import (
    JobProcessingClaim as AnalysisClaim,
    JobProcessingLease as AnalysisLease,
    LostJobProcessingLease as LostAnalysisLease,
    require_job_processing_lease,
)

__all__ = ["AnalysisClaim", "AnalysisLease", "LostAnalysisLease", "require_analysis_lease"]


def require_analysis_lease(
    claim: AnalysisClaim, lease: AnalysisLease | None, *, now: datetime
) -> None:
    """Проверить именно analyze по nonce и времени базы; иначе запретить запись."""
    require_job_processing_lease(claim, lease, kind=JobKind.ANALYZE, now=now)
