"""Совместимый запуск worker; реализация команды находится в src.presentation."""

from src.presentation.cli.worker import main as main

if __name__ == "__main__":
    raise SystemExit(main())
