"""Аргументы и точка входа команды worker."""

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Описать флаги команды; создание парсера не открывает внешние ресурсы."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lanes", default="daily", help="полосы через запятую: daily,backfill")
    parser.add_argument("--once", action="store_true", help="обработать одно задание и выйти")
    parser.add_argument(
        "--stages",
        default="",
        help=(
            "этапы через запятую: ingest,transcribe,analyze. "
            "Пусто — все три. Ограничение нужно, когда этап заведомо "
            "невозможен: например, приложение LLM ещё не создано"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Разобрать аргументы CLI, вызвать сборку сценария и вернуть код выхода."""
    args = build_parser().parse_args(argv)
    from src.di.worker import run_worker

    return run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
