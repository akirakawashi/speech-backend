"""Совместимый запуск seed_call_reasons; реализация команды находится в src.presentation."""

from src.presentation.cli.seed_call_reasons import main as main

if __name__ == "__main__":
    raise SystemExit(main())
