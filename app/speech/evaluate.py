"""Совместимый запуск evaluate; реализация команды находится в src.presentation."""

from src.presentation.cli.evaluate import main as main

if __name__ == "__main__":
    raise SystemExit(main())
