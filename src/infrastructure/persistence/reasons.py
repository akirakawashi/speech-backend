"""PostgreSQL-адаптер пакетного заполнения причин без долгих транзакций."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.application.analysis.storage import with_reason
from src.application.dto.analysis_storage import AnalysisReferencePayload, ReferenceCallReason
from src.application.dto.analysis_writes import (
    AnalysisVersion,
    LegacyAnalysisFields,
    ReasonSnapshot,
    SaveReason,
    TranscriptState,
)
from src.application.dto.pipeline_input import AnalysisSegment
from src.application.dto.reasons import CallReason, ReasonCandidates, ReasonSelection
from src.infrastructure.persistence.analysis_results import AnalysisResultStore

_CANDIDATES_SQL = text(
    """
    SELECT c.id
      FROM speech_calls AS c
      JOIN speech_transcripts AS t ON t.call_id = c.id
     WHERE c.started_at >= :date_from
       AND c.started_at <  :date_to
       AND c.call_reason_code IS NULL
       AND EXISTS (SELECT 1 FROM speech_transcript_segments AS s WHERE s.call_id = c.id)
     ORDER BY c.started_at DESC, c.id DESC
     LIMIT :limit
    """
)

_RECENTLY_TRANSCRIBED_SQL = text(
    """
    SELECT c.id
      FROM speech_calls AS c
      JOIN speech_transcripts AS t ON t.call_id = c.id
     WHERE t.created_at >= :transcribed_since
       AND c.call_reason_code IS NULL
       AND EXISTS (SELECT 1 FROM speech_transcript_segments AS s WHERE s.call_id = c.id)
     ORDER BY c.started_at DESC, c.id DESC
     LIMIT :limit
    """
)

_REASONS_SQL = text(
    "SELECT code, label, hint FROM speech_call_reasons WHERE is_active ORDER BY sort_order, code"
)

_SEGMENTS_SQL = text(
    "SELECT seq, speaker, text_raw FROM speech_transcript_segments "
    "WHERE call_id = :call_id ORDER BY seq"
)

_INPUT_SQL = text(
    "SELECT t.id AS transcript_id, t.created_at AS transcript_created_at, t.role_method, "
    "c.call_reason_code IS NOT NULL AS has_reason, "
    "c.ai_node_id, c.ai_suggestion, c.ai_suggestion_node_id, c.sentiment, c.summary, "
    "CASE WHEN c.segment_source = 'llm' THEN c.segment END AS llm_segment, "
    "CASE WHEN c.product_source = 'llm' THEN c.product END AS llm_product "
    "FROM speech_calls c JOIN speech_transcripts t ON t.call_id = c.id WHERE c.id = :call_id"
)

_STORE_SQL = text(
    """
    UPDATE speech_calls
       SET call_reason = :call_reason, call_reason_code = :call_reason_code, updated_at = now()
     WHERE id = :call_id AND call_reason_code IS NULL
    """
)


async def _snapshot(session: AsyncSession, call_id: UUID) -> ReasonSnapshot | None:
    """Прочитать согласованный вход и ожидаемую запись в транзакции вызывающего."""
    params = {"call_id": call_id}
    row = (await session.execute(_INPUT_SQL, params)).mappings().first()
    if row is None:
        return None
    segments = tuple(
        AnalysisSegment.model_validate(dict(item))
        for item in (await session.execute(_SEGMENTS_SQL, params)).mappings()
    )
    reasons = tuple(
        CallReason.model_validate(dict(item))
        for item in (await session.execute(_REASONS_SQL)).mappings()
    )
    current = await AnalysisResultStore(session).current(call_id)
    return ReasonSnapshot(
        call_id=call_id,
        transcript=TranscriptState(
            transcript_id=row["transcript_id"],
            transcript_created_at=row["transcript_created_at"],
            role_method=row["role_method"],
            segments=segments,
        ),
        reasons=reasons,
        expected=AnalysisVersion(id=current.id, revision=current.revision) if current else None,
        legacy=LegacyAnalysisFields.model_validate(
            {name: row[name] for name in LegacyAnalysisFields.model_fields}
        ),
        has_reason=bool(row["has_reason"] or (current and current.call_reason_code is not None)),
    )


class PostgresReasonRepository:
    """Открывать отдельную сессию на чтение или запись, без ожидания модели в транзакции."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Принять фабрику сессий базы речи."""
        self._sessions = sessions

    async def candidates(self, selection: ReasonSelection) -> ReasonCandidates:
        """Выполнить безопасный отбор и вернуть DTO звонков и справочника."""
        if selection.transcribed_since is not None:
            query = _RECENTLY_TRANSCRIBED_SQL
            params = {"transcribed_since": selection.transcribed_since, "limit": selection.limit}
        else:
            query = _CANDIDATES_SQL
            params = {
                "date_from": selection.date_from,
                "date_to": selection.date_to,
                "limit": selection.limit,
            }
        async with self._sessions() as session:
            ids = tuple(UUID(str(row[0])) for row in (await session.execute(query, params)).all())
            reasons = tuple(
                CallReason.model_validate(dict(row))
                for row in (await session.execute(_REASONS_SQL)).mappings()
            )
        return ReasonCandidates(call_ids=ids, reasons=reasons)

    async def load(self, call_id: UUID) -> ReasonSnapshot | None:
        """Прочитать REPEATABLE READ снимок и освободить соединение до LLM."""
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            return await _snapshot(session, call_id)

    async def save(self, command: SaveReason) -> bool:
        """Записать причину и происхождение только при прежнем входе и версии.

        Args:
            command: Ответ, факты запроса и исходный снимок до LLM.

        Returns:
            True после commit обеих проекций; False при изменении входа,
            готовой причине или занятых данных. Статусы полного анализа не меняются.

        Raises:
            Exception: При ошибке хранения; транзакция целиком откатывается.
        """
        try:
            async with self._sessions() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout = 3000"))
                params = {"call_id": command.source.call_id}
                call = await session.scalar(
                    text("SELECT id FROM speech_calls WHERE id = :call_id FOR UPDATE"), params
                )
                if call is None:
                    return False
                await session.execute(
                    text(
                        "SELECT id FROM speech_transcripts "
                        "WHERE call_id = :call_id FOR UPDATE NOWAIT"
                    ),
                    params,
                )
                await session.execute(
                    text(
                        "SELECT seq FROM speech_transcript_segments "
                        "WHERE call_id = :call_id FOR UPDATE NOWAIT"
                    ),
                    params,
                )
                await session.execute(text("LOCK TABLE speech_call_reasons IN SHARE MODE NOWAIT"))
                current_input = await _snapshot(session, command.source.call_id)
                source = command.source
                if (
                    current_input is None
                    or current_input.has_reason
                    or current_input.fingerprint != source.fingerprint
                    or current_input.expected != source.expected
                    or (source.expected is None and current_input.legacy != source.legacy)
                ):
                    return False
                store = AnalysisResultStore(session)
                current = await store.current(source.call_id)
                reference = AnalysisReferencePayload(
                    scenario="reason",
                    reason_reference_state="sent",
                    reasons=tuple(
                        ReferenceCallReason.model_validate(reason.model_dump())
                        for reason in source.reasons
                    ),
                )
                reference_id = await store.reference(reference)
                now = (await session.execute(text("SELECT clock_timestamp()"))).scalar_one()
                await store.put(with_reason(command, current, reference_id, now))
                await session.execute(
                    _STORE_SQL,
                    {
                        "call_id": source.call_id,
                        "call_reason": command.result.answer.text,
                        "call_reason_code": command.result.answer.code,
                    },
                )
            return True
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01"}:
                return False
            raise
