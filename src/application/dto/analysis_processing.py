"""Снимок анализа и параметры самостоятельного CPU-процесса без секретов."""

import hashlib
import json
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.application.dto.job_processing import JobProcessingOutcome, JobProcessorOptions
from src.application.dto.pipeline_input import AnalysisInput
from src.application.ports.queue import ClaimedJob
from src.domain.calls.analysis_processing import (
    AnalysisClaim as AnalysisClaim,
    AnalysisLease as AnalysisLease,
)
from src.domain.calls.contracts import JobKind


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


class AnalysisOutcome(JobProcessingOutcome):
    """Исход анализа после commit либо потери захвата."""


class AnalysisProcessorOptions(JobProcessorOptions):
    """Полосы и once для сохранённой команды только analyze."""


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
