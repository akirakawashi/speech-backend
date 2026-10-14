"""Единый CPU-процесс бэкенда: последовательные analyze/evaluate и постановка оценки."""

import argparse
from collections.abc import Sequence

from pydantic import ValidationError

from src.application.dto.job_processing import SpeechProcessorOptions


def build_parser() -> argparse.ArgumentParser:
    """Описать этапы, полосы, once и отключение автоматической постановки оценки."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stages", default="analyze,evaluate", help="этапы через запятую: analyze,evaluate"
    )
    parser.add_argument(
        "--lanes", default="daily,backfill", help="полосы через запятую: daily,backfill"
    )
    parser.add_argument(
        "--once", action="store_true", help="обработать не больше одного задания и выйти"
    )
    parser.add_argument(
        "--sources",
        dest="ingest_sources",
        default="ncc,telphin",
        help="автоматический приём метаданных: ncc,telphin; пустая строка отключает приём",
    )
    parser.add_argument(
        "--no-schedule",
        dest="schedule_evaluations",
        action="store_false",
        help="отключить автоматическую постановку оценки; приём задаётся через --sources",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Проверить ввод как DTO и вызвать DI; неверные аргументы дают код 2 до I/O."""
    parser = build_parser()
    try:
        options = SpeechProcessorOptions.model_validate(vars(parser.parse_args(argv)))
    except ValidationError as exc:
        parser.error(str(exc))
    from src.di.speech_processor import run

    return run(options)


if __name__ == "__main__":
    raise SystemExit(main())
