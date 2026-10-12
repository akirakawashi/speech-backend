"""Сборка клиента Keycloak из настроек Speech Analytics.

Механика протокола переехала в `src/infrastructure/auth/oidc.py`: та же задача встала перед
основным приложением, и держать её внутри одного из сервисов значило бы
заставить второй импортировать чужую реализацию. `app/auth/` объявлен общей
инфраструктурой, которой может пользоваться любой контекст.

Здесь осталось единственное, что у сервиса своё, — какие поля настроек читать.
Общий модуль про них знать не должен, иначе перестанет быть общим.

Имена протокола переэкспортируются, поэтому вызывающий код не изменился: он
по-прежнему пишет `oidc.exchange_code(...)`, `oidc.OidcError` и так далее.
Форма `as` обязательна — mypy проверяется с `no_implicit_reexport`.
"""

from __future__ import annotations

from src.infrastructure.auth.oidc import (
    OIDC_TIMEOUT_SECONDS as OIDC_TIMEOUT_SECONDS,
    PKCE_VERIFIER_BYTES as PKCE_VERIFIER_BYTES,
    OidcClient as OidcClient,
    OidcError as OidcError,
    build_authorize_url as build_authorize_url,
    build_logout_url as build_logout_url,
    exchange_code as exchange_code,
    new_pkce_verifier as new_pkce_verifier,
    pkce_challenge as pkce_challenge,
    refresh_tokens as refresh_tokens,
)
from src.setting.config import Settings


def client_from_settings(settings: Settings) -> OidcClient:
    """Клиент Keycloak по настройкам речевой аналитики (`SPEECH_KEYCLOAK_*`)."""
    return OidcClient(
        base_url=str(settings.speech_keycloak_url or "").strip(),
        realm=str(settings.speech_keycloak_realm or "").strip(),
        client_id=str(settings.speech_keycloak_client_id or "").strip(),
        client_secret=str(settings.speech_keycloak_client_secret or "").strip(),
    )
