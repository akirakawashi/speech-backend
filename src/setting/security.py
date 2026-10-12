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
    """Отказаться стартовать, если `Secure` снят там, где снимать нельзя.

    Зовётся из ОБОИХ приложений — `Settings.validate_runtime_security`
    (бэкенд) и `Settings.validate_speech_runtime` (`speech-ms`). Раньше
    проверка жила только в первой, а `speech-ms` — отдельный контейнер
    (`docker-compose.yml`, `target: speech-ms`), который её не звал: он
    поднимался с любым `APP_ENV` и `SESSION_COOKIE_SECURE=never`, раздавая
    `speech_session`, `speech_login` и `speech_audio` без `Secure`. Это ровно
    та находка пентеста (COOKIE-SEC, WSTG-SESS), от которой защищает флаг.

    Почему запрет только для продуктива, а не «везде, кроме local».

    Вне продуктива `never` — это ЯВНОЕ объявление оператора, а не вывод из
    заголовка, которым управляет чужой прокси; именно поэтому его вообще можно
    разрешить, не возвращая прежнюю дыру. А нужен он потому, что прямой вход
    по `http`, минуя корпоративный прокси, на стенде поддерживается: так
    сказано в `aicistent-frontend/deploy/nginx.conf` (белый список значений
    `X-Forwarded-Proto` заведён с оговоркой «прямой отладочный вход на
    10.220.21.1:8080 работает как раньше») и в
    `aicistent-documentation/operations/2026-09-21-ops-request-hsts.md`.
    Безусловный `Secure` на альфе этот вход ломает молча: браузер куку по
    `http` не возвращает, `/callback` не находит `speech_login`/
    `aicistent_login` и отвечает `303` с `auth_error=state_mismatch` —
    бесконечный цикл входа, неотличимый от сбоя Keycloak. То же касается
    прослушивания записей (`speech_audio`) на `http://10.220.21.1:8081`.

    Продуктив при этом защищён абсолютно: там `never` не стартует вовсе, и
    обойти это можно только объявив `APP_ENV` не продуктивом — то есть
    попутно включив dev-вход, `/docs` и режим `open` у речёвки, чего в проде
    не заметить нельзя.
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
