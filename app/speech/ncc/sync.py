"""Совместимый запуск синхронизации справочников NCC."""

from src.presentation.cli.directory_sync import main as main

if __name__ == "__main__":
    raise SystemExit(main())
