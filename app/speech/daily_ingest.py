"""Совместимый запуск daily_ingest; реализация команды находится в src.presentation."""

from src.presentation.cli.daily_ingest import main as main

if __name__ == "__main__":
    raise SystemExit(main())
