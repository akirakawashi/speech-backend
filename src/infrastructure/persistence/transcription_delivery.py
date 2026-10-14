"""Короткая транзакция PostgreSQL: распознавание, анализ и квитанция в payload задания."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.application.dto.transcription_delivery import (
    ReceivedTranscription,
    ReceivingCall,
    StoredTranscriptionReceipt,
    TranscriptionClaim,
    TranscriptionLease,
)
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.queue import (
    TRANSCRIPTION_RECEIPT_KEY,
    NewJob,
    claim_token_from_payload,
)
from src.application.ports.transcription_delivery import TranscriptionReceptionTransaction
from src.infrastructure.persistence.analysis_results import remove_analysis
from src.infrastructure.persistence.settings_store import read_setting
from src.infrastructure.persistence.stages.transcribe import store_transcript
from src.infrastructure.queue.postgres import CALL_LOCK_TIMEOUT_MS

_LOCK_CALL = text(
    "SELECT id, audio_status, audio_object_key, audio_channels, processing_status "
    "FROM speech_calls WHERE id = :call_id FOR UPDATE"
)
_LOCK_JOBS = text(
    "SELECT id, call_id, kind, status, lane, priority, attempts, locked_by, payload, "
    "locked_at + make_interval(secs => lease_seconds) AS lease_until "
    "FROM speech_jobs WHERE call_id = :call_id ORDER BY id FOR UPDATE NOWAIT"
)
_INVALIDATE_CALL = text(
    """
    UPDATE speech_calls SET processing_status = 'analyzing', completed_at = NULL,
        analysis_status = 'pending', sentiment = NULL, summary = NULL,
        ai_node_id = NULL, ai_suggestion = NULL, ai_suggestion_node_id = NULL,
        call_reason = NULL, call_reason_code = NULL,
        segment = CASE WHEN segment_source = 'project' THEN segment ELSE NULL END,
        segment_source = CASE WHEN segment_source = 'project' THEN 'project' ELSE 'unknown' END,
        product = CASE WHEN product_source = 'project' THEN product ELSE NULL END,
        product_source = CASE WHEN product_source = 'project' THEN 'project' ELSE 'unknown' END,
        updated_at = clock_timestamp()
    WHERE id = :call_id
    """
)
_ENQUEUE_ANALYSIS = text(
    """
    INSERT INTO speech_jobs (
        id, call_id, kind, lane, status, priority, attempts, max_attempts,
        lease_seconds, payload, run_after, created_at
    ) VALUES (
        gen_random_uuid(), :call_id, 'analyze', :lane, 'queued', :priority,
        0, :max_attempts, :lease_seconds, '{}'::jsonb, clock_timestamp(), clock_timestamp()
    ) ON CONFLICT (call_id, kind) DO UPDATE SET
        status = 'queued', lane = EXCLUDED.lane, priority = EXCLUDED.priority,
        attempts = 0, max_attempts = EXCLUDED.max_attempts,
        lease_seconds = EXCLUDED.lease_seconds, payload = '{}'::jsonb,
        run_after = EXCLUDED.run_after, locked_by = NULL, locked_at = NULL,
        started_at = NULL, finished_at = NULL, last_error_class = NULL, last_error_at = NULL
    WHERE speech_jobs.status <> 'running'
    RETURNING id
    """
)
_COMPLETE = text(
    """
    UPDATE speech_jobs SET status = 'done', finished_at = clock_timestamp(),
        locked_by = NULL, locked_at = NULL,
        payload = (payload - '_transcription_delivery') || CAST(:receipt AS jsonb)
    WHERE id = :job_id AND call_id = :call_id AND kind = 'transcribe'
      AND status = 'running' AND locked_by = :worker_id AND attempts = :attempt
      AND payload ->> '_claim_token' = :claim_token
      AND locked_at + make_interval(secs => lease_seconds) > clock_timestamp()
    RETURNING id
    """
)


class PostgresTranscriptionReceptionRepository:
    """Создать единицу работы приёма с rollback на отказе и commit перед подтверждением."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Принять фабрику сессий; соединение создаётся только внутри транзакции."""
        self._sessions = sessions

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[TranscriptionReceptionTransaction]:
        """Открыть одну транзакцию; занятые блокировки вернуть как безопасный CONFLICT."""
        try:
            async with self._sessions() as session, session.begin():
                await session.execute(text(f"SET LOCAL lock_timeout = {CALL_LOCK_TIMEOUT_MS}"))
                yield PostgresTranscriptionReceptionTransaction(session)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in ("55P03", "40P01"):
                raise ApplicationError(
                    ErrorKind.CONFLICT, "transcription_pipeline_is_busy"
                ) from None
            raise


class PostgresTranscriptionReceptionTransaction:
    """Операции приёма в сессии владельца; ни один метод не делает commit."""

    def __init__(self, session: AsyncSession) -> None:
        """Принять уже открытую транзакцию."""
        self._session = session

    async def lock_call(self, call_id: UUID) -> ReceivingCall | None:
        """Заблокировать звонок и вернуть DTO его входа либо None."""
        row = (await self._session.execute(_LOCK_CALL, {"call_id": call_id})).mappings().first()
        return ReceivingCall.model_validate(dict(row)) if row is not None else None

    async def lock_jobs(self, call_id: UUID) -> tuple[TranscriptionLease, ...]:
        """Вернуть снимки заданий под NOWAIT-блокировками, сохранив порядок блокировок."""
        rows = (await self._session.execute(_LOCK_JOBS, {"call_id": call_id})).mappings().all()
        return tuple(
            TranscriptionLease.model_validate(
                {**dict(row), "claim_token": claim_token_from_payload(row["payload"])}
            )
            for row in rows
        )

    async def receipt(self, job_id: UUID) -> StoredTranscriptionReceipt | None:
        """Прочитать квитанцию без второго текста; повреждённые метаданные не перезаписывать."""
        value = await self._session.scalar(
            text("SELECT payload -> '_transcription_receipt' FROM speech_jobs WHERE id = :job_id"),
            {"job_id": job_id},
        )
        if value is None:
            return None
        try:
            return StoredTranscriptionReceipt.model_validate(value)
        except ValidationError:
            raise ApplicationError(
                ErrorKind.DEPENDENCY, "transcription_receipt_is_invalid"
            ) from None

    async def database_time(self) -> datetime:
        """Прочитать clock_timestamp, включая время ожидания блокировок."""
        result = await self._session.scalar(text("SELECT clock_timestamp()"))
        if not isinstance(result, datetime):
            raise RuntimeError("database did not return its clock")
        return result

    async def analysis_attempts(self) -> int:
        """Вернуть лимит анализа из действующих настроек; отсутствие использует прежний предел 3."""
        value = await read_setting(self._session, "max_attempts")
        return int(value.get("analyze", 3))

    async def invalidate_results(self, call_id: UUID) -> None:
        """Снять прежнюю разметку, оценку и неработающие задания оценки до замены реплик."""
        params = {"call_id": call_id}
        await remove_analysis(self._session, call_id)
        await self._session.execute(_INVALIDATE_CALL, params)
        await self._session.execute(
            text("DELETE FROM speech_call_evaluations WHERE call_id = :call_id"), params
        )
        await self._session.execute(
            text(
                "DELETE FROM speech_jobs WHERE call_id = :call_id AND kind = 'evaluate' "
                "AND status <> 'running'"
            ),
            params,
        )

    async def save_transcription(self, call_id: UUID, result: ReceivedTranscription) -> UUID:
        """Заменить текст существующим SQL-путём и сохранить digest модели."""
        return await store_transcript(
            self._session, call_id, result, model_digest=result.model_digest
        )

    async def enqueue_analysis(self, job: NewJob) -> UUID:
        """Поставить свежий анализ с унаследованной полосой и срочностью."""
        job_id = await self._session.scalar(
            _ENQUEUE_ANALYSIS,
            {
                "call_id": job.call_id,
                "lane": job.lane.value,
                "priority": job.priority,
                "max_attempts": job.max_attempts,
                "lease_seconds": job.lease_seconds,
            },
        )
        if job_id is None:
            raise ApplicationError(ErrorKind.CONFLICT, "transcription_pipeline_is_busy")
        return UUID(str(job_id))

    async def complete(
        self, claim: TranscriptionClaim, receipt: StoredTranscriptionReceipt
    ) -> bool:
        """Завершить только ту же живую аренду и добавить квитанцию в существующий payload."""
        job_id = await self._session.scalar(
            _COMPLETE,
            {
                "job_id": claim.job_id,
                "call_id": claim.call_id,
                "worker_id": claim.worker_id,
                "attempt": claim.attempt,
                "claim_token": str(claim.claim_token),
                "receipt": json.dumps({TRANSCRIPTION_RECEIPT_KEY: receipt.model_dump(mode="json")}),
            },
        )
        return job_id is not None
