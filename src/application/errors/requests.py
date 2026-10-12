"""Ошибки сценариев, которые точки входа переводят в свой транспорт."""

from enum import StrEnum


class ErrorKind(StrEnum):
    """Смысл отказа независимо от HTTP-кода."""

    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    FORBIDDEN = "forbidden"
    UNAUTHORIZED = "unauthorized"
    CONFLICT = "conflict"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    DEPENDENCY = "dependency"
    INTERNAL = "internal"


class ApplicationError(Exception):
    """Ожидаемый отказ сценария с устойчивым кодом причины."""

    def __init__(self, kind: ErrorKind, detail: str, *, challenge: bool = False) -> None:
        """Принять смысл отказа, причину и необходимость повторного входа."""
        self.kind = kind
        self.detail = detail
        self.challenge = challenge
        super().__init__(detail)
