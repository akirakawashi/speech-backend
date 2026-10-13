"""PostgreSQL: согласованный вход и nonce-защищённая запись CPU-оценки."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.calls import JobKind, JobStatus
from src.application.dto.evaluation_processing import EvaluationInput, EvaluationSnapshot
from src.application.dto.job_processing import JobProcessingClaim, job_processing_conflict
from src.application.dto.quality import TranscriptLine
from src.application.dto.quality_execution import QualityWork
from src.application.ports.evaluation_processing import EvaluationProcessingTransaction
from src.infrastructure.persistence.job_processing import (
    PostgresJobProcessingRepository,
    PostgresJobProcessingTransaction,
    check_current_claim,
    processing_transaction,
)
from src.infrastructure.persistence.stages.evaluate import store_result

_INPUT = text(
    "SELECT c.source, c.processing_status, t.id, t.created_at, t.role_method "
    "FROM speech_calls c JOIN speech_transcripts t ON t.call_id = c.id WHERE c.id = :call_id"
)
_LINES = text(
    "SELECT seq, speaker, start_ms, text_raw FROM speech_transcript_segments "
    "WHERE call_id = :call_id ORDER BY seq"
)
_OTHER_JOBS = text(
    "SELECT kind, status FROM speech_jobs WHERE call_id = :call_id AND id <> :job_id"
)
_LOCK_TRANSCRIPT = text(
    "SELECT id FROM speech_transcripts WHERE call_id = :call_id FOR UPDATE NOWAIT"
)
_LOCK_LINES = text(
    "SELECT seq FROM speech_transcript_segments WHERE call_id = :call_id FOR UPDATE NOWAIT"
)
_NEWER_SUCCESS = text(
    "SELECT id FROM speech_call_evaluations WHERE call_id = :call_id "
    "AND script_version = :script_version AND evaluated_at > :read_at "
    "AND failure_reason IS NULL FOR UPDATE NOWAIT"
)


async def _input(session: AsyncSession, call_id: UUID) -> EvaluationInput | None:
    """Прочитать идентичность/метаданные транскрипта и реплики из сессии владельца."""
    row = (await session.execute(_INPUT, {"call_id": call_id})).mappings().first()
    if row is None:
        return None
    lines = (await session.execute(_LINES, {"call_id": call_id})).mappings().all()
    return EvaluationInput(
        call_source=row["source"],
        processing_status=row["processing_status"],
        transcript_id=row["id"],
        transcript_created_at=row["created_at"],
        role_method=row["role_method"],
        lines=tuple(
            TranscriptLine(
                seq=line["seq"],
                speaker=line["speaker"],
                start_ms=line["start_ms"],
                text=line["text_raw"],
            )
            for line in lines
        ),
    )


class PostgresEvaluationProcessingRepository(PostgresJobProcessingRepository):
    """Закрывать снимок до HTTP и открывать короткую UoW только для записи."""

    async def load(self, claim: JobProcessingClaim) -> EvaluationSnapshot | None:
        """Вернуть вход живого evaluate в REPEATABLE READ, не удерживая сессию при LLM."""
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            maximum = await check_current_claim(session, claim, kind=JobKind.EVALUATE)
            read_at: datetime = (
                await session.execute(text("SELECT clock_timestamp()"))
            ).scalar_one()
            data = await _input(session, claim.call_id)
            if data is None:
                return None
            others = (
                (
                    await session.execute(
                        _OTHER_JOBS,
                        {
                            "call_id": claim.call_id,
                            "job_id": claim.job_id,
                        },
                    )
                )
                .mappings()
                .all()
            )
            result = EvaluationSnapshot(
                data=data,
                read_at=read_at,
                max_attempts=maximum,
                other_processing=any(
                    job_processing_conflict(
                        JobKind.EVALUATE, JobKind(row["kind"]), JobStatus(row["status"])
                    )
                    for row in others
                ),
            )
        return result

    def transaction(self) -> AbstractAsyncContextManager[EvaluationProcessingTransaction]:
        """Открыть атомарную запись оценки и задания с общими правилами блокировок."""
        return processing_transaction(self._sessions, PostgresEvaluationProcessingTransaction)


class PostgresEvaluationProcessingTransaction(PostgresJobProcessingTransaction):
    """Записывать оценку и роли без изменения готовности или completed_at звонка."""

    async def snapshot(self, call_id: UUID) -> EvaluationInput | None:
        """Удержать транскрипт и реплики NOWAIT до commit, включая вставки дочерних строк."""
        await self._session.execute(_LOCK_TRANSCRIPT, {"call_id": call_id})
        await self._session.execute(_LOCK_LINES, {"call_id": call_id})
        return await _input(self._session, call_id)

    async def newer_success(self, call_id: UUID, script_version: str, read_at: datetime) -> bool:
        """Защитить уже появившуюся более новую успешную оценку от позднего результата."""
        return (
            await self._session.scalar(
                _NEWER_SUCCESS,
                {
                    "call_id": call_id,
                    "script_version": script_version,
                    "read_at": read_at,
                },
            )
            is not None
        )

    async def save(self, claim: JobProcessingClaim, work: QualityWork) -> None:
        """Переиспользовать существующий SQL проверки ролей, итога и нормализованного отказа."""
        await store_result(self._session, claim.call_id, work)
