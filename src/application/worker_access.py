"""Проверка доступа самостоятельного воркера к служебным сценариям."""

import hmac

from src.application.errors.requests import ApplicationError, ErrorKind


def authorize_worker(configured_token: str, authorization: str | None) -> None:
    """Проверить Bearer-токен; пустая настройка даёт 503, неверный токен — 401.

    Значения токенов не включаются в ошибку. Проверка не обращается к сети.
    """
    if not configured_token:
        raise ApplicationError(ErrorKind.UNAVAILABLE, "worker access is not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        supplied.encode(), configured_token.encode()
    ):
        raise ApplicationError(ErrorKind.UNAUTHORIZED, "worker access denied", challenge=True)
