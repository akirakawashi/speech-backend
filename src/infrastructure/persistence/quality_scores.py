"""Балл звонка там, где оценка не главное: в выдаче поиска.

Отдельный модуль, а не функция в `quality_routes`: тот импортирует `routes`
(сессию, разбор периода), и обратный импорт замкнул бы цикл.

Балл есть только у действующей оценки — по тем же условиям, что карточка и
сводка в `quality_routes`: текущая редакция скрипта, его источник звонков,
ответ модели принят, звонок с тех пор не переобработан. У звонка вне скрипта
балла нет.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.quality import CURRENT_SCRIPT, CallScript

_SCORES_SQL = text(
    """
    SELECT e.call_id, e.score
      FROM speech_call_evaluations AS e
      JOIN speech_calls AS c ON c.id = e.call_id
      JOIN speech_transcripts AS t ON t.call_id = e.call_id
     WHERE e.call_id = ANY(CAST(:call_ids AS uuid[]))
       AND e.script_version = :script_version
       AND c.source = :source
       AND e.evaluated_at >= t.created_at
       AND e.failure_reason IS NULL
       AND e.script_applies
       AND e.score IS NOT NULL
    """
)


async def call_scores(
    session: AsyncSession,
    call_ids: Sequence[uuid.UUID],
    *,
    source: str,
    script: CallScript = CURRENT_SCRIPT,
) -> dict[uuid.UUID, int]:
    """Баллы звонков из списка. Звонка без действующей оценки в ответе нет.

    `source` — источник, по которому шёл поиск. Звонкам другого источника балл
    не положен, и в базу за ними не ходят: поиск по NCC не должен платить
    запросом за оценку, которой у его звонков не бывает.
    """
    if not call_ids or source != script.source.value:
        return {}
    result = await session.execute(
        _SCORES_SQL,
        {
            "call_ids": list(call_ids),
            "script_version": script.version,
            "source": script.source.value,
        },
    )
    return {row["call_id"]: int(row["score"]) for row in result.mappings()}
