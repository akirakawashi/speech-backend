"""Снимок анализа и параметры самостоятельного CPU-процесса без секретов."""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from src.application.dto.pipeline_input import AnalysisInput
from src.application.ports.queue import ClaimedJob
from src.domain.calls.analysis_processing import (
    AnalysisClaim as AnalysisClaim,
    AnalysisLease as AnalysisLease,
)
from src.domain.calls.contracts import JobErrorClass, JobKind, JobLane


class AnalysisSnapshot(BaseModel):
    """Согласованный снимок расшифровки и справочников одной короткой транзакции."""

    model_config = ConfigDict(frozen=True)
    transcript_id: UUID
    transcript_created_at: AwareDatetime
    data: AnalysisInput = Field(repr=False)

    @property
    def fingerprint(self) -> str:
        """Хешировать вход, роли и справочники; порядок строк SELECT не влияет на результат."""
        value = self.model_dump(mode="json")
        value["data"]["classifier"] = sorted(value["data"]["classifier"], key=lambda row: row["id"])
        value["data"]["reasons"] = sorted(value["data"]["reasons"], key=lambda row: row["code"])
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AnalysisOutcome(BaseModel):
    """Результат обработки после commit; текст и ответ LLM в отчёт не входят."""

    model_config = ConfigDict(frozen=True)
    status: Literal["done", "retry", "dead", "stale", "deferred"]
    error_class: JobErrorClass | None = None


class AnalysisProcessorOptions(BaseModel):
    """Проверить полосы и режим once; этот процесс всегда берёт только analyze."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    lanes: tuple[JobLane, ...] = Field(default=(JobLane.DAILY, JobLane.BACKFILL), min_length=1)
    once: bool = False

    @field_validator("lanes", mode="before")
    @classmethod
    def parse_lanes(cls, value: Any) -> Any:
        """Прочитать строку полос через запятую; пустое значение отклоняется Pydantic."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("lanes")
    @classmethod
    def unique_lanes(cls, value: tuple[JobLane, ...]) -> tuple[JobLane, ...]:
        """Удалить дубли, сохранив явно выбранный порядок."""
        return tuple(dict.fromkeys(value))


def analysis_claim(job: ClaimedJob, worker_id: str) -> AnalysisClaim:
    """Сформировать команду из захвата очереди; другой этап или отсутствие nonce дают ValueError."""
    if job.kind is not JobKind.ANALYZE or job.claim_token is None:
        raise ValueError("analysis processor requires a claimed analyze job with nonce")
    return AnalysisClaim(
        call_id=job.call_id,
        job_id=job.id,
        claim_token=job.claim_token,
        worker_id=worker_id,
        attempt=job.attempts,
    )
