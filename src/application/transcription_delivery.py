"""Приём распознавания: проверка задания, атомарная запись и безопасный повтор доставки."""

import hashlib
import json

from src.application.dto.transcription_delivery import (
    StoredTranscriptionReceipt,
    TranscriptionDeliveryRequest,
    TranscriptionReceipt,
)
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.queue import NewJob
from src.application.ports.transcription_delivery import TranscriptionReceptionRepository
from src.application.worker_access import authorize_worker
from src.domain.calls.contracts import JobKind, JobStatus
from src.domain.calls.transcription_delivery import (
    StaleTranscriptionResult,
    validate_transcription_claim,
)


class TranscriptionDeliveryService:
    """Принять результат и подтвердить commit; не обращаться к Whisper или LLM."""

    def __init__(self, repository: TranscriptionReceptionRepository, *, worker_token: str) -> None:
        """Внедрить порт атомарной записи и ключ служебного доступа."""
        self._repository = repository
        self._worker_token = worker_token

    async def receive_authorized(
        self, command: TranscriptionDeliveryRequest, authorization: str | None
    ) -> TranscriptionReceipt:
        """Проверить запрос и вернуть квитанцию только после commit или точного повтора.

        Принимает DTO распознавания и Bearer-заголовок. Отсутствующий звонок
        или задание даёт NOT_FOUND, устаревший/противоречивый результат —
        CONFLICT. Любой отказ до commit откатывает запись и постановку анализа.
        """
        authorize_worker(self._worker_token, authorization)
        payload_hash = _payload_hash(command)
        claim = command.claim
        async with self._repository.transaction() as transaction:
            call = await transaction.lock_call(claim.call_id)
            if call is None:
                raise ApplicationError(ErrorKind.NOT_FOUND, "transcription_call_is_unavailable")
            jobs = await transaction.lock_jobs(claim.call_id)
            job = next((job for job in jobs if job.id == claim.job_id), None)
            if job is None:
                raise ApplicationError(ErrorKind.NOT_FOUND, "transcription_job_is_unavailable")
            stored = await transaction.receipt(job.id)
            if stored is not None:
                if (
                    job.status is not JobStatus.DONE
                    or job.claim_token != claim.claim_token
                    or stored.receipt.claim_token != claim.claim_token
                    or stored.receipt.job_id != claim.job_id
                    or stored.receipt.call_id != claim.call_id
                    or stored.receipt.attempt != claim.attempt
                    or stored.payload_sha256 != payload_hash
                ):
                    raise ApplicationError(
                        ErrorKind.CONFLICT, "transcription_receipt_does_not_match"
                    )
                return stored.receipt
            now = await transaction.database_time()
            try:
                validate_transcription_claim(claim, command.source, call, job, now=now)
            except StaleTranscriptionResult as exc:
                raise ApplicationError(ErrorKind.CONFLICT, str(exc)) from None
            if any(
                other.id != job.id
                and (
                    other.status is JobStatus.RUNNING
                    or (other.kind is JobKind.INGEST and other.status is JobStatus.QUEUED)
                )
                for other in jobs
            ):
                raise ApplicationError(ErrorKind.CONFLICT, "transcription_pipeline_is_busy")
            await transaction.invalidate_results(claim.call_id)
            transcript_id = await transaction.save_transcription(claim.call_id, command.result)
            analysis_job_id = await transaction.enqueue_analysis(
                NewJob(
                    call_id=claim.call_id,
                    kind=JobKind.ANALYZE,
                    lane=job.lane,
                    priority=job.priority,
                    max_attempts=await transaction.analysis_attempts(),
                    lease_seconds=300,
                )
            )
            receipt = TranscriptionReceipt(
                call_id=claim.call_id,
                job_id=claim.job_id,
                claim_token=claim.claim_token,
                attempt=claim.attempt,
                transcript_id=transcript_id,
                analysis_job_id=analysis_job_id,
                accepted_at=await transaction.database_time(),
            )
            if not await transaction.complete(
                claim, StoredTranscriptionReceipt(payload_sha256=payload_hash, receipt=receipt)
            ):
                raise ApplicationError(ErrorKind.CONFLICT, "transcription_claim_is_stale")
        return receipt


def _payload_hash(command: TranscriptionDeliveryRequest) -> str:
    """Хешировать канонический DTO для повтора; не хранить вторую копию текста."""
    payload = json.dumps(
        command.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
