"""Совместимый ASGI-вход для local-dev/run.sh; новая точка входа — фабрика DI."""

from src.di.http import create_app as create_app

app = create_app()
