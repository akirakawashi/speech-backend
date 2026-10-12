"""Разговор с Keycloak на стороне сервера — общий для обоих приложений.

Появился в Speech Analytics (ADR-SPEECH-0002) и переехал сюда, когда та же
задача встала перед основным приложением: выданные клиенты **конфиденциальные**,
а секрет в браузер положить нельзя — значит обмен кода на токен делает сервис.

Лежит в `app/auth/`, а не внутри одного из сервисов, намеренно: этот пакет
объявлен общей инфраструктурой (`SHARED_PREFIXES` в
`scripts/verify/context-imports.py`), которой может пользоваться любой
ограниченный контекст. Модуль внутри `app/speech_service/` был бы для основного
приложения чужой реализацией, и импорт в него — той самой связностью, которую
этот гейт ищет.

Здесь только механика протокола и ни одной настройки: `OidcClient` собирается
вызывающим из **его** настроек. Иначе общий модуль знал бы про поля обоих
приложений и перестал бы быть общим.

Проверки токена здесь нет: она в `src/infrastructure/auth/keycloak.py` — с кэшем JWKS,
перезапросом на неизвестный `kid` и проверкой издателя с аудиторией. Второй
разбор токена — второе место, где можно ошибиться в проверке подписи.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any, Final
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict

#: Потолок ожидания Keycloak. Вход — интерактивное действие: человек смотрит в
#: экран, и тридцать секунд ожидания он трактует как «сломалось».
OIDC_TIMEOUT_SECONDS: Final[float] = 10.0

#: Длина проверочного кода PKCE в байтах. RFC 7636 требует от 43 до 128
#: символов после base64url; 64 байта дают 86.
PKCE_VERIFIER_BYTES: Final[int] = 64


class OidcError(RuntimeError):
    """Keycloak отказал, и код отказа важен вызывающему.

    `invalid_grant` при обновлении означает «сессия погашена на той стороне» —
    человек вышел или его отключили, и нашу строку надо гасить тоже. Все
    остальные коды — это сбой, а не отзыв, и путать их нельзя: иначе временная
    недоступность Keycloak выкидывала бы из системы всех разом.
    """

    def __init__(self, code: str, description: str, *, status: int | None = None) -> None:
        """Настроить OidcError из переданных параметров и зависимостей."""
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description
        self.status = status


class OidcClient(BaseModel):
    """Адреса realm и реквизиты клиента.

    Отдельный объект, а не чтение настроек по месту: адреса собираются из
    `url` и `realm` в четырёх местах, и собранные по-разному они разошлись бы
    молча — например, с завершающей косой чертой в одном месте и без неё в
    другом.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    base_url: str
    realm: str
    client_id: str
    client_secret: str

    @property
    def realm_url(self) -> str:
        """Вернуть базовый адрес настроенного realm OIDC."""
        return f"{self.base_url.rstrip('/')}/realms/{self.realm}"

    @property
    def authorize_endpoint(self) -> str:
        """Вернуть адрес начала авторизации OIDC для настроенного realm."""
        return f"{self.realm_url}/protocol/openid-connect/auth"

    @property
    def token_endpoint(self) -> str:
        """Вернуть адрес обмена токенами настроенного OIDC-провайдера."""
        return f"{self.realm_url}/protocol/openid-connect/token"

    @property
    def end_session_endpoint(self) -> str:
        """Вернуть адрес завершения сессии настроенного OIDC-провайдера."""
        return f"{self.realm_url}/protocol/openid-connect/logout"

    @property
    def is_configured(self) -> bool:
        """Проверить наличие обязательных параметров OIDC-клиента."""
        return bool(self.base_url and self.realm and self.client_id and self.client_secret)


def new_pkce_verifier() -> str:
    """Создать случайный секрет PKCE для одной попытки входа."""
    return base64.urlsafe_b64encode(secrets.token_bytes(PKCE_VERIFIER_BYTES)).decode().rstrip("=")


def pkce_challenge(verifier: str) -> str:
    """Получить публичный SHA-256 challenge из секрета PKCE."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def build_authorize_url(
    client: OidcClient,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    scope: str = "openid profile email",
) -> str:
    """Адрес, на который отправляется браузер.

    PKCE отправляется, **хотя клиент конфиденциальный**. Он ничего не стоит и
    закрывает перехват кода на участке до сервиса; а если в клиенте выставлено
    требование S256, без него вход просто не состоится — и выяснилось бы это на
    живом пользователе.
    """
    query = urlencode(
        {
            "client_id": client.client_id,
            "response_type": "code",
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{client.authorize_endpoint}?{query}"


def build_logout_url(
    client: OidcClient, *, post_logout_redirect_uri: str, id_token_hint: str | None = None
) -> str:
    """Адрес выхода из Keycloak.

    `id_token_hint` передаётся, когда он есть: без него Keycloak показывает
    страницу подтверждения «вы точно хотите выйти», и выход из приложения
    выглядит как зависший переход.
    """
    params: dict[str, str] = {
        "client_id": client.client_id,
        "post_logout_redirect_uri": post_logout_redirect_uri,
    }
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    return f"{client.end_session_endpoint}?{urlencode(params)}"


async def _post_token(client: OidcClient, form: dict[str, str]) -> dict[str, Any]:
    """Вызвать endpoint токенов OIDC и вернуть проверенный ответ."""
    try:
        async with httpx.AsyncClient(timeout=OIDC_TIMEOUT_SECONDS) as http:
            response = await http.post(
                client.token_endpoint,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        # Недоступный Keycloak — это не «плохой код». Разница видна выше:
        # по ней решается, гасить сессию или ответить «попробуйте позже».
        raise OidcError("provider_unavailable", str(exc)) from exc

    if response.status_code >= 400:
        code, description = "invalid_request", ""
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict):
            code = str(body.get("error") or code)
            description = str(body.get("error_description") or "")
        raise OidcError(code, description, status=response.status_code)

    try:
        tokens = response.json()
    except ValueError as exc:
        raise OidcError("invalid_response", "token endpoint returned a non-JSON body") from exc
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise OidcError("invalid_response", "token endpoint returned no access_token")
    return tokens


async def exchange_code(
    client: OidcClient, *, code: str, redirect_uri: str, code_verifier: str
) -> dict[str, Any]:
    """Обменять код на токены. Это и есть тот шаг, который не может браузер."""
    return await _post_token(
        client,
        {
            "grant_type": "authorization_code",
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )


async def refresh_tokens(client: OidcClient, *, refresh_token: str) -> dict[str, Any]:
    """Обновить токены — и заодно узнать, жив ли человек в Keycloak."""
    return await _post_token(
        client,
        {
            "grant_type": "refresh_token",
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "refresh_token": refresh_token,
        },
    )
