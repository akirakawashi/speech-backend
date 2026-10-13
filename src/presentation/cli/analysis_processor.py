"""Команда CPU-обработчика analyze проекта speech-backend."""

import argparse
from collections.abc import Sequence

from pydantic import ValidationError

from src.application.dto.analysis_processing import AnalysisProcessorOptions


def build_parser() -> argparse.ArgumentParser:
    """Описать полосы и once; выбора аудио/оценки у этого процесса нет."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lanes", default="daily,backfill", help="полосы через запятую: daily,backfill"
    )
    parser.add_argument(
        "--once", action="store_true", help="обработать не больше одного analyze и выйти"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Проверить вход CLI как DTO и вызвать сборку; неверные аргументы дают код 2 до I/O."""
    parser = build_parser()
    try:
        options = AnalysisProcessorOptions.model_validate(vars(parser.parse_args(argv)))
    except ValidationError as exc:
        parser.error(str(exc))
    from src.di.analysis_processor import run

    return run(options)


if __name__ == "__main__":
    raise SystemExit(main())
