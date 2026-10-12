"""Журнал действий пользователя HTTP-сервиса.

Правила записи — общие для всего Speech Analytics и живут в
[`src/infrastructure/persistence/audit.py`](../speech/audit.py); здесь только переход от
`Principal`, которого знает сервис, к `AuditActor`, которого знает журнал.

Разделение не декоративное: в тот же журнал пишет плановое удаление записей по
сроку хранения, а оно выполняется в образе воркера и о `Principal` не знает.
Вторая реализация правил обрезки payload означала бы, что одна из них однажды
начнёт хранить то, что вторая прячет.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.application.authorization import Principal
from src.infrastructure.persistence.audit import (
    MAX_PAYLOAD_KEYS,
    MAX_VALUE_LENGTH,
    AuditActor,
    UnknownAuditEvent,
    normalize_payload,
    record_action,
)

__all__ = [
    "MAX_PAYLOAD_KEYS",
    "MAX_VALUE_LENGTH",
    "UnknownAuditEvent",
    "normalize_payload",
    "record",
]


async def record(
    session: AsyncSession,
    principal: Principal,
    event_type: str,
    *,
    call_id: Any = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Записать действие пользователя в транзакции вызывающего."""
    await record_action(
        session,
        AuditActor(
            sub=principal.sub,
            name=principal.display_name or principal.username,
            roles=tuple(principal.roles),
        ),
        event_type,
        call_id=call_id,
        payload=payload,
    )
