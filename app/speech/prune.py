"""Совместимый запуск prune; реализация команды находится в src.presentation."""

from src.presentation.cli.prune import main as main

if __name__ == "__main__":
    raise SystemExit(main())
