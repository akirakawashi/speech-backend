"""Совместимый запуск backfill; реализация команды находится в src.presentation."""

from src.presentation.cli.backfill import main as main

if __name__ == "__main__":
    raise SystemExit(main())
