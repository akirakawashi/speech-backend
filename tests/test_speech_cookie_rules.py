"""Флаг `Secure` на куках сервиса: правило запуска и таблица решений.

В монорепозитории эти проверки стояли в `tests/test_ms_hardening.py` рядом с
проверками основного приложения: модуль `src/presentation/http/cookies.py` у них общий.
Сюда перенесено то, что относится к `speech-ms` и к самому модулю, — иначе
правило запуска этого сервиса оставалось бы здесь без единой проверки.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from src.presentation.http.cookies import cookie_secure
from src.setting.config import Settings


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def _keycloak(**overrides: Any) -> Settings:
    """Режим `keycloak` со всеми реквизитами: упасть может только правило куки.

    `open` вне `local` и `alpha` запрещён сам по себе, и тест правила куки
    зеленел бы на чужом исключении.
    """
    return _settings(
        speech_auth_mode="keycloak",
        speech_keycloak_issuer_url="https://ssoc.example/realms/IC-GROUP",
        speech_keycloak_jwks_url="https://ssoc.example/realms/IC-GROUP/protocol/openid-connect/certs",
        speech_keycloak_audience="speech",
        speech_keycloak_url="https://ssoc.example",
        speech_keycloak_realm="IC-GROUP",
        speech_keycloak_client_id="speech",
        speech_keycloak_client_secret="s3cret",
        speech_public_base_url="https://speech.ic-group.ru",
        **overrides,
    )


def _request(*, client_host: str) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/me",
        "headers": [],
        "query_string": b"",
        "client": (client_host, 51234),
    }
    return Request(scope)


def test_the_service_refuses_to_start_with_the_flag_off_in_production() -> None:
    """Без этой проверки сервис поднимался с `SESSION_COOKIE_SECURE=never` в продуктиве.

    И раздавал `speech_session`, `speech_login` и `speech_audio` без `Secure` —
    находка пентеста, от которой флаг защищает.
    """
    settings = _keycloak(app_env="prod", session_cookie_secure="never")

    with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE"):
        settings.validate_speech_runtime()


def test_a_stand_may_declare_the_flag_off_explicitly() -> None:
    """На стенде явное `never` — отладочный режим для прямого входа по `http`, а не дыра."""
    _keycloak(app_env="alpha", session_cookie_secure="never").validate_speech_runtime()


def test_production_starts_with_the_default_flag() -> None:
    _keycloak(app_env="prod").validate_speech_runtime()


def test_an_unknown_value_is_refused_at_parse_time() -> None:
    """`"alwais"` не должно молча означать `auto`: опечатка отвергается, а не толкуется."""
    with pytest.raises(ValidationError):
        _settings(session_cookie_secure="alwais")


@pytest.mark.parametrize(
    ("app_env", "mode", "expected"),
    [
        ("alpha", "auto", True),
        ("local", "auto", False),
        ("alpha", "never", False),
        ("local", "always", True),
    ],
)
def test_the_flag_follows_the_declared_environment(app_env: str, mode: str, expected: bool) -> None:
    """Таблица решений `cookie_secure` целиком, без заголовков.

    Заголовка `X-Forwarded-Proto` здесь нет намеренно: вне `local` он не
    участвует вовсе, а в `local` его отсутствие означает `http`.
    """
    settings = _settings(app_env=app_env, session_cookie_secure=mode)

    assert cookie_secure(settings, _request(client_host="10.0.0.1")) is expected
