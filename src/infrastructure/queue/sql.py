"""Общие SQL-выражения очереди и наблюдения за временем ожидания."""

from typing import Final

from src.application.ports.queue import NOT_BEFORE_KEY

WAITING_SINCE_SQL: Final[str] = (
    f"GREATEST(created_at, CAST(payload->>'{NOT_BEFORE_KEY}' AS timestamptz))"
)
