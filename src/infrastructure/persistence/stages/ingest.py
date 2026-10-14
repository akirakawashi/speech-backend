"""Чтение метаданных звонка и проекта для бизнес-решений служебного ingest API."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.pipeline_input import (
    CallForIngest,
)

_LOAD_CALL_SQL = text(
    """
    SELECT c.session_id, c.duration_s, c.started_at, c.audio_status,
           c.audio_object_key, c.source_snapshot, c.source, c.source_audio_url,
           p.segment AS project_segment, p.product AS project_product
      FROM speech_calls AS c
      LEFT JOIN speech_ncc_projects AS p ON p.id = c.project_id
     WHERE c.id = :call_id
    """
)


async def load(session: AsyncSession, call_id: UUID) -> CallForIngest | None:
    """Прочитать метаданные записи и вернуть DTO; None означает удалённый звонок."""
    row = (await session.execute(_LOAD_CALL_SQL, {"call_id": call_id})).mappings().first()
    return CallForIngest.model_validate(dict(row)) if row is not None else None
