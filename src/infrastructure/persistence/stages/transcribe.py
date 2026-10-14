"""Сохранение принятого DTO распознавания в транзакции служебного API."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.pipeline import TranscribeWork

_DELETE_TRANSCRIPT_SQL = text("DELETE FROM speech_transcripts WHERE call_id = :call_id")


_INSERT_TRANSCRIPT_SQL = text(
    """
    INSERT INTO speech_transcripts (
        id, call_id, engine, model_name, model_digest, language, role_method,
        duration_s, segments_count, created_at
    )
    VALUES (
        gen_random_uuid(), :call_id, :engine, :model_name, :model_digest, :language,
        :role_method, :duration_s, :segments_count, now()
    )
    RETURNING id
    """
)


_INSERT_SEGMENT_SQL = text(
    """
    INSERT INTO speech_transcript_segments (
        transcript_id, call_id, seq, speaker, start_ms, end_ms, text_raw
    )
    VALUES (
        :transcript_id, :call_id, :seq, :speaker, :start_ms, :end_ms, :text_raw
    )
    """
)


async def store_transcript(
    session: AsyncSession,
    call_id: UUID,
    work: TranscribeWork,
    *,
    model_digest: str | None = None,
) -> UUID:
    """Заменить реплики в переданной транзакции и вернуть UUID без собственного commit."""
    await session.execute(_DELETE_TRANSCRIPT_SQL, {"call_id": call_id})
    transcript_id = (
        await session.execute(
            _INSERT_TRANSCRIPT_SQL,
            {
                "call_id": call_id,
                "engine": work.engine,
                "model_name": work.model_name,
                "model_digest": model_digest,
                "language": work.language,
                "role_method": work.role_method.value,
                "duration_s": work.duration_s,
                "segments_count": len(work.replies),
            },
        )
    ).scalar_one()
    for reply in work.replies:
        await session.execute(
            _INSERT_SEGMENT_SQL,
            {
                "transcript_id": transcript_id,
                "call_id": call_id,
                "seq": reply.seq,
                "speaker": reply.speaker.value,
                "start_ms": reply.start_ms,
                "end_ms": reply.end_ms,
                "text_raw": reply.text,
            },
        )
    return UUID(str(transcript_id))
