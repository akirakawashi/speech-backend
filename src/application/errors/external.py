"""Нормализованные ошибки внешних операций, доступные сценариям."""

from __future__ import annotations


class StorageError(Exception):
    """Отказ хранилища. Тело ответа наружу не выносится."""

    def __init__(
        self, message: str, *, reason: str = "provider_error", status_code: int | None = None
    ) -> None:
        """Настроить StorageError из переданных параметров и зависимостей."""
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


class StorageNotConfiguredError(StorageError):
    """Хранилище не настроено — это не сбой, а незаконченная настройка."""


class AnalysisError(Exception):
    """Воркфлоу не ответил или ответил не тем.

    Текст ответа наружу не выносится: в нём лежит разговор целиком (BR-13).
    """

    def __init__(self, reason: str, *, retryable: bool) -> None:
        """Настроить AnalysisError из переданных параметров и зависимостей."""
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class AnalysisNotConfigured(RuntimeError):
    """Адрес или ключ приложения не заданы — ошибка развёртывания."""
