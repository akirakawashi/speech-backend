"""PostgreSQL-порты CPU-анализа: снимок до LLM и атомарная запись после него."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.application.dto.analysis_processing import AnalysisClaim, AnalysisLease, AnalysisSnapshot
from src.application.dto.calls import JobErrorClass, JobKind, JobLane, JobStatus
from src.application.dto.pipeline import AnalyzeWork
from src.application.errors.analysis_processing import AnalysisProcessingBusy, LostAnalysisLease
from src.application.ports.analysis_processing import AnalysisProcessingTransaction
from src.application.ports.queue import ExpiredJob, claim_token_from_payload
from src.infrastructure.persistence.stages import analyze
from src.infrastructure.queue.postgres import backoff_seconds

_LIVE_CLAIM = """
    id = :job_id AND call_id = :call_id AND kind = 'analyze' AND status = 'running'
    AND locked_by = :worker_id AND attempts = :attempt
    AND payload->>'_claim_token' = :claim_token
    AND locked_at + make_interval(secs => lease_seconds) > clock_timestamp()
"""
_CHECK_CLAIM = text("SELECT id FROM speech_jobs WHERE " + _LIVE_CLAIM)
_COMPLETE = text(
    "UPDATE speech_jobs SET status = 'done', finished_at = clock_timestamp(), "
    "locked_by = NULL, locked_at = NULL WHERE " + _LIVE_CLAIM + " RETURNING id"
)
_FAIL = text(
    """
    UPDATE speech_jobs
       SET status = CASE WHEN :terminal OR attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
           run_after = clock_timestamp() + make_interval(secs => :backoff),
           locked_by = NULL, locked_at = NULL,
           payload = payload - '_claim_token' - '_transcription_receipt',
           last_error_class = :error_class, last_error_at = clock_timestamp(),
           finished_at = CASE WHEN :terminal OR attempts >= max_attempts
                             THEN clock_timestamp() ELSE NULL END
     WHERE """
    + _LIVE_CLAIM
    + " RETURNING status"
)
_DEFER = text(
    """
    UPDATE speech_jobs
       SET status = 'queued', attempts = GREATEST(attempts - 1, 0),
           run_after = clock_timestamp() + interval '60 seconds',
           locked_by = NULL, locked_at = NULL,
           payload = payload - '_claim_token' - '_transcription_receipt'
     WHERE """
    + _LIVE_CLAIM
    + " RETURNING id"
)
_LOCK_CALL = text("SELECT id FROM speech_calls WHERE id = :call_id FOR UPDATE")
_LOCK_JOBS = text(
    "SELECT id, call_id, kind, status, attempts, locked_by, locked_at, lease_seconds, payload "
    "FROM speech_jobs WHERE call_id = :call_id ORDER BY id FOR UPDATE NOWAIT"
)
_TRANSCRIPT = text("SELECT id, created_at FROM speech_transcripts WHERE call_id = :call_id")
_LOCK_TRANSCRIPT = text(
    "SELECT id FROM speech_transcripts WHERE call_id = :call_id FOR UPDATE NOWAIT"
)
_LOCK_SEGMENTS = text(
    "SELECT seq FROM speech_transcript_segments WHERE call_id = :call_id FOR UPDATE NOWAIT"
)
_LOCK_REFERENCES = text(
    "LOCK TABLE speech_classifier_nodes, speech_call_reasons IN SHARE MODE NOWAIT"
)
_DONE = text(
    "UPDATE speech_calls SET processing_status = 'done', completed_at = clock_timestamp(), "
    "updated_at = clock_timestamp() WHERE id = :call_id"
)
_FAILED = text(
    "UPDATE speech_calls SET processing_status = 'done', analysis_status = 'failed', "
    "completed_at = COALESCE(completed_at, clock_timestamp()), "
    "updated_at = clock_timestamp() WHERE id = :call_id"
)

_UNPROJECTED_FAILURES = text(
    "SELECT j.id, j.call_id, j.kind, j.status FROM speech_jobs j "
    "JOIN speech_calls c ON c.id = j.call_id "
    "WHERE j.kind = 'analyze' AND j.status = 'dead' AND j.last_error_class = 'lease_expired' "
    "AND j.lane = ANY(CAST(:lanes AS text[])) "
    "AND (c.analysis_status <> 'failed' OR c.processing_status <> 'done') "
    "ORDER BY j.id"
)


def _parameters(claim: AnalysisClaim) -> dict[str, object]:
    """Собрать технические параметры SQL без текста и секретов."""
    return {
        "job_id": claim.job_id,
        "call_id": claim.call_id,
        "worker_id": claim.worker_id,
        "claim_token": str(claim.claim_token),
        "attempt": claim.attempt,
    }


async def _snapshot(session: AsyncSession, call_id: UUID) -> AnalysisSnapshot | None:
    """Прочитать идентичность транскрипта, реплики и справочники в сессии вызывающего."""
    transcript = (await session.execute(_TRANSCRIPT, {"call_id": call_id})).mappings().first()
    if transcript is None:
        return None
    data = await analyze.load(session, call_id)
    if data is None:
        return None
    return AnalysisSnapshot(
        transcript_id=transcript["id"],
        transcript_created_at=transcript["created_at"],
        data=data,
    )


class PostgresAnalysisProcessingRepository:
    """Открывать короткие сессии; не удерживать их при HTTP-вызове LLM."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Внедрить фабрику сессий, созданную в event loop обработчика."""
        self._sessions = sessions

    async def load(self, claim: AnalysisClaim) -> AnalysisSnapshot | None:
        """Прочитать согласованный снимок живого захвата; устаревший не вызывает LLM."""
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            if await session.scalar(_CHECK_CLAIM, _parameters(claim)) is None:
                raise LostAnalysisLease("analysis_claim_is_stale")
            result = await _snapshot(session, claim.call_id)
        return result

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AnalysisProcessingTransaction]:
        """Открыть UoW; занятая блокировка даёт отсрочку, другие ошибки выходят вызывающему."""
        try:
            async with self._sessions() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout = 3000"))
                yield PostgresAnalysisProcessingTransaction(session)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01"}:
                raise AnalysisProcessingBusy("analysis_data_is_busy") from None
            raise

    async def defer(self, claim: AnalysisClaim) -> bool:
        """Отложить один живой захват после rollback занятой/изменившейся транзакции."""
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout = 3000"))
            deferred = await session.scalar(_DEFER, _parameters(claim))
        return deferred is not None

    async def unprojected_failures(self, lanes: Sequence[JobLane]) -> Sequence[ExpiredJob]:
        """Прочитать неотражённые отказы analyze; актуальность повторно проверяется под lock."""
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        _UNPROJECTED_FAILURES, {"lanes": [lane.value for lane in lanes]}
                    )
                )
                .mappings()
                .all()
            )
        return [
            ExpiredJob(id=row["id"], call_id=row["call_id"], kind=row["kind"], status=row["status"])
            for row in rows
        ]


class PostgresAnalysisProcessingTransaction:
    """Записать анализ, состояния и исход попытки в одной транзакции."""

    def __init__(self, session: AsyncSession) -> None:
        """Принять сессию UoW; самостоятельно commit не выполняется."""
        self._session = session

    async def lock(self, job_id: UUID, call_id: UUID) -> AnalysisLease | None:
        """Заблокировать звонок, затем задания без встречного ожидания; вернуть состояние."""
        if await self._session.scalar(_LOCK_CALL, {"call_id": call_id}) is None:
            return None
        jobs = (await self._session.execute(_LOCK_JOBS, {"call_id": call_id})).mappings().all()
        job = next((row for row in jobs if row["id"] == job_id), None)
        if job is None:
            return None
        return AnalysisLease(
            call_id=call_id,
            job_id=job_id,
            kind=job["kind"],
            status=job["status"],
            claim_token=claim_token_from_payload(dict(job["payload"])),
            locked_by=job["locked_by"],
            attempt=job["attempts"],
            lease_until=job["locked_at"] + timedelta(seconds=job["lease_seconds"])
            if job["locked_at"] is not None
            else None,
            other_processing=any(
                row["id"] != job_id
                and (
                    row["status"] == "running"
                    or (row["status"] == "queued" and row["kind"] in {"ingest", "transcribe"})
                )
                for row in jobs
            ),
        )

    async def database_time(self) -> datetime:
        """Вернуть clock_timestamp после ожидания блокировок."""
        now: datetime = await self._session.scalar(text("SELECT clock_timestamp()"))
        return now

    async def snapshot(self, call_id: UUID) -> AnalysisSnapshot | None:
        """Удерживать текст, роли и справочники до commit, включая добавление новых узлов."""
        await self._session.execute(_LOCK_TRANSCRIPT, {"call_id": call_id})
        await self._session.execute(_LOCK_SEGMENTS, {"call_id": call_id})
        await self._session.execute(_LOCK_REFERENCES)
        return await _snapshot(self._session, call_id)

    async def save(self, claim: AnalysisClaim, result: AnalyzeWork) -> None:
        """Переиспользовать прежний SQL записи, включая проектные значения и снятие оценки."""
        await analyze.store_result(self._session, claim.call_id, result)

    async def complete(self, claim: AnalysisClaim) -> bool:
        """Проверить nonce/lease ещё раз и записать done звонка в той же транзакции."""
        if await self._session.scalar(_COMPLETE, _parameters(claim)) is None:
            return False
        await self._session.execute(_DONE, {"call_id": claim.call_id})
        return True

    async def fail(
        self, claim: AnalysisClaim, error_class: JobErrorClass, *, terminal: bool
    ) -> JobStatus | None:
        """Сохранить класс отказа и прежний backoff; устаревший захват ничего не меняет."""
        status = await self._session.scalar(
            _FAIL,
            {
                **_parameters(claim),
                "error_class": error_class.value,
                "terminal": terminal,
                "backoff": backoff_seconds(JobKind.ANALYZE, error_class, claim.attempt),
            },
        )
        return JobStatus(status) if status is not None else None

    async def mark_failed(self, call_id: UUID) -> None:
        """Зафиксировать неудавшийся анализ, сохранив текст и статус доступного звонка."""
        await self._session.execute(_FAILED, {"call_id": call_id})
