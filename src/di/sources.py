"""Сборка источников метаданных для суточного приёма; аудио получает отдельный воркер."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.application.ports.sources import DailyBatchSource
from src.domain.calls.contracts import CallSource

if TYPE_CHECKING:  # pragma: no cover
    from src.setting.config import Settings


def build_ingest_source(call_source: str, settings: Settings) -> DailyBatchSource:
    """Источник суточного прогона — с перечислением звонков."""
    if call_source == CallSource.TELPHIN:
        from src.infrastructure.sources.telphin.source import build_telphin_source  # noqa: PLC0415

        return build_telphin_source(settings)
    from src.infrastructure.sources.ncc.source import build_daily_batch_source  # noqa: PLC0415

    return build_daily_batch_source(settings)
