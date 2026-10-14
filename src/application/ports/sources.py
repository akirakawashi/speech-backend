"""Контракт перечисления метаданных звонков без получения аудио."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol, runtime_checkable

from src.application.dto.sources import CallRef


@runtime_checkable
class DailyBatchSource(Protocol):
    """Источник суточного пакета."""

    @property
    def name(self) -> str:
        """Короткое имя источника — попадает в журналы и в панель админа."""
        ...

    async def list_calls(self, day: date) -> Sequence[CallRef]:
        """Все звонки за указанную дату.

        Порядок не важен, дубли недопустимы. Пустой результат — валидный ответ
        (выходной день), он не должен отличаться от ошибки на уровне типа:
        ошибка обязана быть исключением.
        """
        ...
