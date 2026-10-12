"""Допустимые режимы входа и параметры защиты сессионных cookies."""

from __future__ import annotations

from typing import Final, Protocol

SPEECH_AUTH_MODES = frozenset({"open", "keycloak"})
SPEECH_OPEN_MODE_ENVIRONMENTS = frozenset({"local", "alpha"})


class CookieSecurityConfig(Protocol):
    """Поля конфигурации, необходимые для проверки безопасности cookies."""

    session_cookie_secure: str
    app_env: str


STRICT_COOKIE_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"prod", "production"})


def assert_cookie_security(settings: CookieSecurityConfig) -> None:
    """Проверить настройки cookies; вернуть None либо поднять RuntimeError.

    Вход — окружение и режим Secure. Значение never запрещено в prod и
    production; в других окружениях его можно задать для доступа по HTTP.
    """
    if str(settings.session_cookie_secure).strip().lower() != "never":
        return
    if settings.app_env.strip().lower() in STRICT_COOKIE_ENVIRONMENTS:
        raise RuntimeError(
            "SESSION_COOKIE_SECURE=never strips the Secure flag from the session "
            "cookie and is refused when APP_ENV is "
            + "/".join(sorted(STRICT_COOKIE_ENVIRONMENTS))
            + ". Use 'auto' (the default) or 'always' in production."
        )
