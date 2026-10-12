"""Атомарный отбор готовых разговоров и дедупликация заданий оценки."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.application.dto.maintenance import QualityQueueReport
from src.application.dto.quality_batch import CandidateSelection

_CANDIDATES = text(
    """
    SELECT c.id, COALESCE(a.lane, 'daily') AS lane,
           GREATEST(COALESCE(a.priority, 20), 30) AS priority
      FROM speech_calls AS c
      JOIN speech_transcripts AS t ON t.call_id = c.id
      LEFT JOIN speech_jobs AS a ON a.call_id = c.id AND a.kind = 'analyze'
      LEFT JOIN speech_jobs AS j ON j.call_id = c.id AND j.kind = 'evaluate'
     WHERE c.source = :source AND c.processing_status = 'done'
       AND (CAST(:call_id AS uuid) IS NULL OR c.id = CAST(:call_id AS uuid))
       AND (CAST(:date_from AS timestamptz) IS NULL OR c.started_at >= :date_from)
       AND (CAST(:date_to AS timestamptz) IS NULL OR c.started_at < :date_to)
       AND (CAST(:force AS boolean) OR NOT EXISTS (
           SELECT 1 FROM speech_call_evaluations AS e
            WHERE e.call_id = c.id AND e.script_version = :script_version
              AND e.evaluated_at >= t.created_at
              AND (e.failure_reason IS NULL OR CAST(:skip_failed AS boolean))
       ))
       AND (j.id IS NULL OR j.status NOT IN ('queued', 'running'))
       AND (NOT CAST(:skip_failed AS boolean) OR j.id IS NULL OR j.status <> 'dead'
            OR t.created_at > COALESCE(j.finished_at, j.created_at)
            OR j.payload ->> 'script_version' IS DISTINCT FROM :script_version)
     ORDER BY c.started_at DESC, c.id DESC
     LIMIT :limit
     FOR UPDATE OF c SKIP LOCKED
    """
)

_ENQUEUE = text(
    """
    INSERT INTO speech_jobs
        (id, call_id, kind, lane, status, priority, attempts, max_attempts,
         run_after, lease_seconds, payload, created_at)
    VALUES (gen_random_uuid(), :call_id, 'evaluate', :lane, 'queued', :priority, 0,
            COALESCE((SELECT (value ->> 'evaluate')::integer FROM speech_settings
                       WHERE key = 'max_attempts'), 3),
            now(), 900, CAST(:payload AS jsonb), now())
    ON CONFLICT (call_id, kind) DO UPDATE
        SET lane = EXCLUDED.lane, status = 'queued', priority = EXCLUDED.priority,
            attempts = 0, max_attempts = EXCLUDED.max_attempts,
            run_after = now(), lease_seconds = EXCLUDED.lease_seconds,
            payload = EXCLUDED.payload, created_at = now(),
            locked_by = NULL, locked_at = NULL, started_at = NULL, finished_at = NULL,
            last_error_class = NULL, last_error_at = NULL
        WHERE speech_jobs.status NOT IN ('queued', 'running')
    RETURNING id
    """
)


class PostgresQualityQueueRepository:
    """Выбирать кандидатов до LIMIT без активных заданий, чтобы очередь не голодала."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        """Принять фабрику коротких транзакций общей базы речи."""
        self._sessions = sessionmaker

    async def schedule(
        self, selection: CandidateSelection, *, call_id: str | None, apply: bool
    ) -> QualityQueueReport:
        """Заблокировать выбранные звонки и создать либо сбросить их терминальные задания.

        Повтор не заменяет активное задание. Dry run не изменяет строки.
        NCC, незавершённые звонки и действующие оценки исключены в SQL.
        """
        async with self._sessions() as session, session.begin():
            rows = (
                (
                    await session.execute(
                        _CANDIDATES,
                        {
                            "source": selection.script.source.value,
                            "call_id": call_id,
                            "date_from": selection.date_from,
                            "date_to": selection.date_to,
                            "force": selection.force,
                            "skip_failed": selection.skip_failed,
                            "script_version": selection.script.version,
                            "limit": selection.limit,
                        },
                    )
                )
                .mappings()
                .all()
            )
            enqueued = 0
            if apply:
                payload = json.dumps({"script_version": selection.script.version})
                for row in rows:
                    result = await session.execute(
                        _ENQUEUE,
                        {
                            "call_id": row["id"],
                            "lane": row["lane"],
                            "priority": row["priority"],
                            "payload": payload,
                        },
                    )
                    enqueued += result.scalar_one_or_none() is not None
        return QualityQueueReport(candidates=len(rows), enqueued=enqueued, applied=apply)
