"""PostgreSQL-порты CPU-анализа: снимок до LLM и атомарная запись после него."""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.analysis.storage import full_analysis
from src.application.dto.analysis_processing import AnalysisClaim, AnalysisSnapshot
from src.application.dto.analysis_writes import SaveAnalysis
from src.application.dto.calls import JobKind, JobLane
from src.application.ports.analysis_processing import AnalysisProcessingTransaction
from src.application.ports.queue import ExpiredJob
from src.infrastructure.persistence.analysis_results import AnalysisResultStore
from src.infrastructure.persistence.job_processing import (
    PostgresJobProcessingRepository,
    PostgresJobProcessingTransaction,
    check_current_claim,
    processing_transaction,
)
from src.infrastructure.persistence.stages import analyze

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


class PostgresAnalysisProcessingRepository(PostgresJobProcessingRepository):
    """Открывать короткие сессии; не удерживать их при HTTP-вызове LLM."""

    async def load(self, claim: AnalysisClaim) -> AnalysisSnapshot | None:
        """Прочитать согласованный снимок живого захвата; устаревший не вызывает LLM."""
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            await check_current_claim(session, claim, kind=JobKind.ANALYZE)
            result = await _snapshot(session, claim.call_id)
        return result

    def transaction(self) -> AbstractAsyncContextManager[AnalysisProcessingTransaction]:
        """Открыть UoW анализа с общими nonce-защищёнными операциями задания."""
        return processing_transaction(self._sessions, PostgresAnalysisProcessingTransaction)

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


class PostgresAnalysisProcessingTransaction(PostgresJobProcessingTransaction):
    """Записать анализ, состояния и исход попытки в одной транзакции."""

    async def snapshot(self, call_id: UUID) -> AnalysisSnapshot | None:
        """Удерживать текст, роли и справочники до commit, включая добавление новых узлов."""
        await self._session.execute(_LOCK_TRANSCRIPT, {"call_id": call_id})
        await self._session.execute(_LOCK_SEGMENTS, {"call_id": call_id})
        await self._session.execute(_LOCK_REFERENCES)
        return await _snapshot(self._session, call_id)

    async def save(self, command: SaveAnalysis) -> None:
        """Записать текущее вычисление и прежние поля вместе в текущей транзакции.

        Args:
            command: Сверенный вход, захват и пригодный результат с фактами запроса.

        Side Effects:
            Сохраняет снимок, анализ, происхождение, проекцию и роли;
            ошибка последующей проверки lease откатывает их все.
        """
        store = AnalysisResultStore(self._session)
        current = await store.current(command.claim.call_id)
        reference_id = await store.reference(command.result.reference)
        result = full_analysis(command, current, reference_id, await self.database_time())
        await store.put(result)
        await analyze.store_result(self._session, command.claim.call_id, command.result.work)

    async def complete(self, claim: AnalysisClaim) -> bool:
        """Проверить nonce/lease ещё раз и записать done звонка в той же транзакции."""
        if not await super().complete(claim):
            return False
        await self._session.execute(_DONE, {"call_id": claim.call_id})
        return True

    async def mark_failed(self, call_id: UUID) -> None:
        """Зафиксировать неудавшийся анализ, сохранив текст и статус доступного звонка."""
        await self._session.execute(_FAILED, {"call_id": call_id})
