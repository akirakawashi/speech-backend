"""Действующий захват задания перед изменением бизнес-результатов."""

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.domain.calls.contracts import JobKind, JobStatus


class JobProcessingClaim(BaseModel):
    """Захват очереди; nonce отличает одинаковые номер попытки и имя процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    call_id: UUID
    job_id: UUID
    claim_token: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(strict=True, ge=1, le=32767)
    kind: JobKind = JobKind.ANALYZE


class JobProcessingLease(BaseModel):
    """Прочитанное состояние задания и конфликтующих этапов под блокировкой."""

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


class LostJobProcessingLease(ValueError):
    """Устаревший, заменённый или просроченный захват не имеет права записи."""


def job_processing_conflict(kind: JobKind, other_kind: JobKind, status: JobStatus) -> bool:
    """Запретить запись при активном этапе или ожидающем предшествующем этапе."""
    upstream = {JobKind.INGEST, JobKind.TRANSCRIBE}
    if kind is JobKind.EVALUATE:
        upstream.add(JobKind.ANALYZE)
    return status is JobStatus.RUNNING or (status is JobStatus.QUEUED and other_kind in upstream)


def require_job_processing_lease(
    claim: JobProcessingClaim,
    lease: JobProcessingLease | None,
    *,
    kind: JobKind,
    now: datetime,
) -> None:
    """Сверить вид, UUID, nonce, владельца, попытку и время базы; иначе запретить запись."""
    if lease is None or (
        claim.kind is not kind
        or lease.call_id != claim.call_id
        or lease.job_id != claim.job_id
        or lease.kind is not kind
        or lease.status is not JobStatus.RUNNING
        or lease.claim_token != claim.claim_token
        or lease.locked_by != claim.worker_id
        or lease.attempt != claim.attempt
        or lease.lease_until is None
        or lease.lease_until <= now
    ):
        raise LostJobProcessingLease("processing_claim_is_stale")
