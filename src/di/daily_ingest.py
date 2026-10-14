"""Сборка ручного приёма метаданных в бэкенде; аудио скачивает отдельный воркер."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from src.application.dto.calls import CallSource, JobLane
from src.di.ingestion import catch_up, run_day
from src.di.sources import build_ingest_source
from src.infrastructure.persistence.session import get_speech_ms_engine, get_speech_ms_sessionmaker
from src.infrastructure.queue.postgres import PostgresJobQueue
from src.infrastructure.runtime.logs import configure_logging
from src.presentation.cli.daily_ingest import _history_from, day_share_of, resolve_day
from src.setting.config import Settings, get_settings


async def _run_daily_command(args: argparse.Namespace, settings: Settings) -> int:
    """В одном event loop собрать порты, принять метаданные и закрыть пул при любом исходе."""
    share = day_share_of(args, settings.speech_display_timezone)
    day = None if args.catch_up else resolve_day(args, settings.speech_display_timezone)
    history_from = (
        _history_from(settings.speech_telphin_history_from)
        if args.source == CallSource.TELPHIN
        else None
    )
    engine = get_speech_ms_engine(settings)
    try:
        sessions = get_speech_ms_sessionmaker(settings)
        source = build_ingest_source(args.source, settings)
        queue = PostgresJobQueue(sessions, engine=engine)
        if args.catch_up:
            lines = await catch_up(
                source=source,
                sessionmaker=sessions,
                queue=queue,
                call_source=args.source,
                today=datetime.now(ZoneInfo(settings.speech_display_timezone)).date(),
                history_from=history_from,
                history_days=settings.speech_telphin_history_days_per_run,
                phone_pepper=settings.speech_phone_hash_pepper,
                day_share=share,
                priority=args.priority,
            )
            for line in lines:
                print(line)
        else:
            assert day is not None
            report = await run_day(
                day,
                source=source,
                sessionmaker=sessions,
                queue=queue,
                lane=JobLane(args.lane),
                limit=args.limit,
                call_source=args.source,
                phone_pepper=settings.speech_phone_hash_pepper,
                day_share=share,
                priority=args.priority,
            )
            print(report.describe())
        return 0
    finally:
        await engine.dispose()


def run_daily_command(args: argparse.Namespace) -> int:
    """Прочитать настройки и выполнить ручной приём с сохранением прежних параметров CLI."""
    configure_logging()
    return asyncio.run(_run_daily_command(args, get_settings()))
