"""Снимок решения ingest в payload задания; блокировки освобождаются до скачивания."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.application.dto.ingest_admission import IngestAdmission, IngestContext
from src.application.dto.ingest_control import IngestClaim
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.ingest_control import IngestControlTransaction
from src.infrastructure.persistence.settings_store import read_runtime_settings
from src.infrastructure.persistence.stages.ingest import load
from src.infrastructure.queue.postgres import CALL_LOCK_TIMEOUT_MS

_ADMISSION_KEY = "_ingest_admission"
_LOCK_JOB = text(
    "SELECT call_id, kind, status, locked_by, attempts, max_attempts, payload, "
    "locked_at + make_interval(secs => lease_seconds) > clock_timestamp() AS alive "
    "FROM speech_jobs WHERE id = :job_id FOR UPDATE NOWAIT"
)
_REMEMBER = text(
    "UPDATE speech_jobs SET payload = payload || CAST(:admission AS jsonb) "
    "WHERE id = :job_id AND call_id = :call_id AND kind = 'ingest' "
    "AND status = 'running' AND locked_by = :worker_id AND attempts = :attempt "
    "AND payload ->> '_claim_token' = :claim_token "
    "AND locked_at + make_interval(secs => lease_seconds) > clock_timestamp() RETURNING id"
)


class PostgresIngestControlRepository:
    """Создавать короткие транзакции решения без HTTP и самостоятельного завершения задания."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Принять фабрику сессий бэкенда."""
        self._sessions = sessions

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[IngestControlTransaction]:
        """Открыть транзакцию; занятость вернуть как CONFLICT, прочие ошибки не скрывать."""
        try:
            async with self._sessions() as session, session.begin():
                await session.execute(text(f"SET LOCAL lock_timeout = {CALL_LOCK_TIMEOUT_MS}"))
                yield PostgresIngestControlTransaction(session)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in ("55P03", "40P01"):
                raise ApplicationError(ErrorKind.CONFLICT, "ingest_pipeline_is_busy") from None
            raise


class PostgresIngestControlTransaction:
    """Проверять конкретную аренду и сохранять решение без URL и текста разговора."""

    def __init__(self, session: AsyncSession) -> None:
        """Принять сессию открытой единицы работы."""
        self._session = session

    async def load(self, claim: IngestClaim) -> IngestContext:
        """Заблокировать звонок, затем задание; чужая/истёкшая аренда даёт CONFLICT."""
        call_id = await self._session.scalar(
            text("SELECT id FROM speech_calls WHERE id = :call_id FOR UPDATE"),
            {"call_id": claim.call_id},
        )
        if call_id is None:
            raise ApplicationError(ErrorKind.NOT_FOUND, "ingest_call_is_missing")
        row = (await self._session.execute(_LOCK_JOB, {"job_id": claim.job_id})).mappings().first()
        if (
            row is None
            or row["call_id"] != claim.call_id
            or row["kind"] != "ingest"
            or row["status"] != "running"
            or row["locked_by"] != claim.worker_id
            or row["attempts"] != claim.attempt
            or row["payload"].get("_claim_token") != str(claim.claim_token)
            or not row["alive"]
        ):
            raise ApplicationError(ErrorKind.CONFLICT, "ingest_claim_changed")
        admission = None
        value = row["payload"].get(_ADMISSION_KEY)
        if value is not None:
            try:
                saved = IngestAdmission.model_validate(value)
            except ValidationError:
                raise ApplicationError(
                    ErrorKind.DEPENDENCY, "ingest_admission_is_invalid"
                ) from None
            if saved.claim_token == claim.claim_token:
                admission = saved
        call = await load(self._session, claim.call_id)
        if call is None:
            raise ApplicationError(ErrorKind.NOT_FOUND, "ingest_call_is_missing")
        configured = await read_runtime_settings(self._session)
        return IngestContext(
            call=call,
            max_attempts=row["max_attempts"],
            short_call_threshold_s=configured["short_call_threshold_s"],
            admission=admission,
        )

    async def remember(self, claim: IngestClaim, admission: IngestAdmission) -> None:
        """Записать решение только для живого захвата; потеря срока откатывает весь снимок."""
        job_id = await self._session.scalar(
            _REMEMBER,
            {
                "job_id": claim.job_id,
                "call_id": claim.call_id,
                "worker_id": claim.worker_id,
                "attempt": claim.attempt,
                "claim_token": str(claim.claim_token),
                "admission": json.dumps({_ADMISSION_KEY: admission.model_dump(mode="json")}),
            },
        )
        if job_id is None:
            raise ApplicationError(ErrorKind.CONFLICT, "ingest_claim_changed")
