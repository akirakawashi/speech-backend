"""Общие DTO CPU-процессов: захват, исход и проверенные аргументы запуска."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.application.dto.calls import CallSource, JobErrorClass, JobKind, JobLane
from src.application.ports.queue import ClaimedJob
from src.domain.calls.job_processing import (
    JobProcessingClaim as JobProcessingClaim,
    JobProcessingLease as JobProcessingLease,
    job_processing_conflict as job_processing_conflict,
)


class JobProcessingOutcome(BaseModel):
    """Исход после commit либо потери захвата; текста и секретов здесь нет."""

    model_config = ConfigDict(frozen=True)
    status: Literal["done", "retry", "dead", "stale", "deferred"]
    error_class: JobErrorClass | None = None


class JobProcessorOptions(BaseModel):
    """Проверить полосы и once до открытия ресурсов CPU-процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    lanes: tuple[JobLane, ...] = Field(default=(JobLane.DAILY, JobLane.BACKFILL), min_length=1)
    once: bool = False

    @field_validator("lanes", mode="before")
    @classmethod
    def parse_lanes(cls, value: Any) -> Any:
        """Прочитать полосы через запятую; пустая строка отклоняется проверкой длины."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("lanes")
    @classmethod
    def unique_lanes(cls, value: tuple[JobLane, ...]) -> tuple[JobLane, ...]:
        """Убрать дубли, сохранив выбранный порядок."""
        return tuple(dict.fromkeys(value))


class SpeechProcessorOptions(JobProcessorOptions):
    """Выбрать анализ/оценку и независимую автоматическую постановку оценки."""

    stages: tuple[JobKind, ...] = Field(default=(JobKind.ANALYZE, JobKind.EVALUATE), min_length=1)
    schedule_evaluations: bool = True
    ingest_sources: tuple[CallSource, ...] = (CallSource.NCC, CallSource.TELPHIN)

    @field_validator("ingest_sources", mode="before")
    @classmethod
    def parse_ingest_sources(cls, value: Any) -> Any:
        """Прочитать источники через запятую; пустая строка отключает автоматический приём."""
        if isinstance(value, str):
            return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
        return value

    @field_validator("stages", mode="before")
    @classmethod
    def parse_stages(cls, value: Any) -> Any:
        """Прочитать названия этапов CLI через запятую."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("stages")
    @classmethod
    def validate_stages(cls, value: tuple[JobKind, ...]) -> tuple[JobKind, ...]:
        """Отклонить этапы аудиоворкера и убрать повторы."""
        if any(kind not in {JobKind.ANALYZE, JobKind.EVALUATE} for kind in value):
            raise ValueError("CPU processor accepts only analyze,evaluate")
        return tuple(dict.fromkeys(value))


def job_processing_claim(job: ClaimedJob, worker_id: str) -> JobProcessingClaim:
    """Преобразовать захваченное модельное задание; отсутствие nonce/другой этап запрещены."""
    if job.kind not in {JobKind.ANALYZE, JobKind.EVALUATE} or job.claim_token is None:
        raise ValueError("CPU processor requires a claimed analyze/evaluate job with nonce")
    return JobProcessingClaim(
        call_id=job.call_id,
        job_id=job.id,
        claim_token=job.claim_token,
        worker_id=worker_id,
        attempt=job.attempts,
        kind=job.kind,
    )
