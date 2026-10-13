"""Общие PostgreSQL-операции захвата и атомарного завершения CPU-заданий."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.application.dto.calls import JobErrorClass, JobKind, JobStatus
from src.application.dto.job_processing import (
    JobProcessingClaim,
    JobProcessingLease,
    job_processing_conflict,
)
from src.application.errors.job_processing import JobProcessingBusy, LostJobProcessingLease
from src.application.ports.queue import claim_token_from_payload
from src.infrastructure.queue.postgres import backoff_seconds

_LIVE = """
    id = :job_id AND call_id = :call_id AND kind = :kind AND status = 'running'
    AND locked_by = :worker_id AND attempts = :attempt
    AND payload->>'_claim_token' = :claim_token
    AND locked_at + make_interval(secs => lease_seconds) > clock_timestamp()
"""
_CHECK = text("SELECT max_attempts FROM speech_jobs WHERE " + _LIVE)
_COMPLETE = text(
    "UPDATE speech_jobs SET status = 'done', finished_at = clock_timestamp(), "
    "locked_by = NULL, locked_at = NULL WHERE " + _LIVE + " RETURNING id"
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
    + _LIVE
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
    + _LIVE
    + " RETURNING id"
)
_LOCK_CALL = text("SELECT id FROM speech_calls WHERE id = :call_id FOR UPDATE")
_LOCK_JOBS = text(
    "SELECT id, call_id, kind, status, attempts, locked_by, locked_at, lease_seconds, payload "
    "FROM speech_jobs WHERE call_id = :call_id ORDER BY id FOR UPDATE NOWAIT"
)


def claim_parameters(claim: JobProcessingClaim) -> dict[str, object]:
    """Собрать параметры SQL только из технических полей захвата."""
    return dict(
        job_id=claim.job_id,
        call_id=claim.call_id,
        kind=claim.kind.value,
        worker_id=claim.worker_id,
        claim_token=str(claim.claim_token),
        attempt=claim.attempt,
    )


async def check_current_claim(
    session: AsyncSession, claim: JobProcessingClaim, *, kind: JobKind
) -> int:
    """Вернуть бюджет живого захвата нужного сценария; иначе LostJobProcessingLease."""
    if claim.kind is not kind:
        raise LostJobProcessingLease("processing_claim_is_stale")
    maximum = await session.scalar(_CHECK, claim_parameters(claim))
    if maximum is None:
        raise LostJobProcessingLease("processing_claim_is_stale")
    return int(maximum)


@asynccontextmanager
async def processing_transaction[Transaction](
    sessions: async_sessionmaker[AsyncSession], factory: Callable[[AsyncSession], Transaction]
) -> AsyncIterator[Transaction]:
    """Открыть UoW; занятость нормализовать для отсрочки, остальные ошибки пропустить."""
    try:
        async with sessions() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout = 3000"))
            yield factory(session)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01"}:
            raise JobProcessingBusy("processing_data_is_busy") from None
        raise


class PostgresJobProcessingRepository:
    """Разделить nonce-защищённую отсрочку между сценариями CPU."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Внедрить фабрику сессий; HTTP внутри этих методов не выполняется."""
        self._sessions = sessions

    async def defer(self, claim: JobProcessingClaim) -> bool:
        """Отложить живую попытку на 60 секунд и вернуть её бюджет; False при смене захвата."""
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout = 3000"))
            result = await session.scalar(_DEFER, claim_parameters(claim))
        return result is not None


class PostgresJobProcessingTransaction:
    """Общие блокировки, время базы и переходы задания в сессии владельца UoW."""

    def __init__(self, session: AsyncSession) -> None:
        """Принять сессию; не выполнять самостоятельный commit."""
        self._session = session

    async def lock(self, job_id: UUID, call_id: UUID) -> JobProcessingLease | None:
        """Заблокировать звонок, затем его задания NOWAIT; вернуть аренду и конфликты."""
        if await self._session.scalar(_LOCK_CALL, {"call_id": call_id}) is None:
            return None
        jobs = (await self._session.execute(_LOCK_JOBS, {"call_id": call_id})).mappings().all()
        job = next((row for row in jobs if row["id"] == job_id), None)
        if job is None:
            return None
        kind = JobKind(job["kind"])
        return JobProcessingLease(
            call_id=call_id,
            job_id=job_id,
            kind=kind,
            status=job["status"],
            claim_token=claim_token_from_payload(dict(job["payload"])),
            locked_by=job["locked_by"],
            attempt=job["attempts"],
            lease_until=job["locked_at"] + timedelta(seconds=job["lease_seconds"])
            if job["locked_at"] is not None
            else None,
            other_processing=any(
                row["id"] != job_id
                and job_processing_conflict(kind, JobKind(row["kind"]), JobStatus(row["status"]))
                for row in jobs
            ),
        )

    async def database_time(self) -> datetime:
        """Вернуть фактическое время базы после ожидания блокировок."""
        value: datetime = (
            await self._session.execute(text("SELECT clock_timestamp()"))
        ).scalar_one()
        return value

    async def complete(self, claim: JobProcessingClaim) -> bool:
        """Повторно проверить живой nonce и записать done; False требует rollback всей UoW."""
        return await self._session.scalar(_COMPLETE, claim_parameters(claim)) is not None

    async def fail(
        self, claim: JobProcessingClaim, error_class: JobErrorClass, *, terminal: bool
    ) -> JobStatus | None:
        """Сохранить прежний backoff/бюджет; вернуть новый статус либо None при потере права."""
        status = await self._session.scalar(
            _FAIL,
            {
                **claim_parameters(claim),
                "error_class": error_class.value,
                "terminal": terminal,
                "backoff": backoff_seconds(claim.kind, error_class, claim.attempt),
            },
        )
        return JobStatus(status) if status is not None else None
