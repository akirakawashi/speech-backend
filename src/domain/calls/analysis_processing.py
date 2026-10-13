"""Актуальность захвата задания анализа перед изменением результатов звонка."""

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.domain.calls.contracts import JobKind, JobStatus


class AnalysisClaim(BaseModel):
    """Выданный очередью захват анализа; nonce отличает даже одинаковый номер попытки."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    call_id: UUID
    job_id: UUID
    claim_token: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(strict=True, ge=1, le=32767)


class AnalysisLease(BaseModel):
    """Состояние задания под блокировкой; вход прочитан из базы, не из команды."""

    model_config = ConfigDict(frozen=True)
    call_id: UUID
    job_id: UUID
    kind: JobKind
    status: JobStatus
    claim_token: UUID | None
    locked_by: str | None
    attempt: int
    lease_until: AwareDatetime | None
    other_processing: bool = False


class LostAnalysisLease(ValueError):
    """Запись запрещена: захват заменён, просрочен или больше не выполняется."""


def require_analysis_lease(
    claim: AnalysisClaim, lease: AnalysisLease | None, *, now: datetime
) -> None:
    """Сверить захват с базой и её временем; вернуть None либо LostAnalysisLease.

    Не изменяет аренду. Идентификатора процесса и номера попытки недостаточно:
    требуется совпадение nonce действующего задания analyze.
    """
    if lease is None or (
        lease.call_id != claim.call_id
        or lease.job_id != claim.job_id
        or lease.kind is not JobKind.ANALYZE
        or lease.status is not JobStatus.RUNNING
        or lease.claim_token != claim.claim_token
        or lease.locked_by != claim.worker_id
        or lease.attempt != claim.attempt
        or lease.lease_until is None
        or lease.lease_until <= now
    ):
        raise LostAnalysisLease("analysis_claim_is_stale")
