"""OIDC, JWT и SQL серверной сессии за портом сценариев входа."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from src.application.authentication.service import safe_next
from src.application.authorization import (
    _ROLE_ORDER,
    Principal,
    open_mode_principal,
    roles_from_groups,
)
from src.application.dto.authentication import (
    BeginLogin,
    Credentials,
    LoginAttempt,
    LoginCallbackQuery,
    LoginCompletion,
    LogoutResult,
)
from src.application.dto.calls import SPEECH_DEFAULT_ROLE_ALIASES
from src.application.errors.requests import ApplicationError, ErrorKind
from src.infrastructure.auth import (
    sessions as sessions,
    speech_oidc as oidc,
)
from src.infrastructure.persistence.users import remember_user
from src.setting.config import Settings

logger = logging.getLogger(__name__)


class _SpeechKeycloakSettings:
    """Переходник: настройки Speech Analytics под именами, которые ждёт валидатор.

    Свой Keycloak у проекта отдельный (решение S-16), и подставлять валидатору
    настройки основного приложения значило бы проверять токены чужого realm —
    молча, потому что синтаксически всё сходится.
    """

    def __init__(self, settings: Settings) -> None:
        """Настроить _SpeechKeycloakSettings из переданных параметров и зависимостей."""
        self.keycloak_issuer_url = settings.speech_keycloak_issuer_url
        self.keycloak_jwks_url = settings.speech_keycloak_jwks_url
        self.keycloak_audience = settings.speech_keycloak_audience
        self.keycloak_role_aliases_json = settings.speech_role_aliases_json or {}
        self.keycloak_super_admin_emails: tuple[str, ...] = ()


def role_aliases(settings: Settings) -> dict[str, tuple[str, ...]]:
    """Соответствие «группа Keycloak → роль сервиса».

    Умолчание — группы, заведённые заказчиком заранее. Переопределённое
    значение **дополняет** умолчание, а не замещает его целиком: переименовав
    одну группу, администратор иначе молча отключил бы две остальные.
    """
    aliases = dict(SPEECH_DEFAULT_ROLE_ALIASES)
    configured = getattr(settings, "speech_role_aliases_json", None) or {}
    if isinstance(configured, str):
        try:
            configured = json.loads(configured or "{}")
        except ValueError:
            configured = {}
    if not isinstance(configured, dict):
        return aliases
    for role, groups in configured.items():
        if str(role) in _ROLE_ORDER and isinstance(groups, (list, tuple)) and groups:
            aliases[str(role)] = tuple(str(group) for group in groups)
    return aliases


async def resolve_principal(settings: Settings, authorization: str | None) -> Principal:
    """Кто выполняет запрос — по режиму сервиса."""
    mode = settings.speech_auth_mode.strip().lower()
    if mode == "open":
        return open_mode_principal()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApplicationError(kind=ErrorKind.UNAUTHORIZED, detail="unauthorized", challenge=True)
    from src.infrastructure.auth.keycloak import KeycloakTokenValidator, token_principals

    validator = KeycloakTokenValidator(_SpeechKeycloakSettings(settings))
    claims = await validator.validate_token(authorization.split(" ", 1)[1].strip())
    groups = token_principals(claims.raw, str(settings.speech_keycloak_audience or ""))
    roles = roles_from_groups(groups, role_aliases(settings))
    if not roles:
        raise ApplicationError(kind=ErrorKind.FORBIDDEN, detail="forbidden")
    return Principal(
        sub=claims.keycloak_sub,
        username=claims.username,
        email=claims.email,
        display_name=claims.display_name,
        roles=roles,
        auth_mode="keycloak",
    )


_TOUCH_MIN_INTERVAL_SECONDS: Final[int] = 60


async def principal_from_session(
    settings: Settings, db: AsyncSession, token: str
) -> Principal | None:
    """Личность по куке сессии, либо `None`, если сессии нет.

    Погашенная, просроченная и несуществующая неотличимы намеренно: снаружи на
    все три случая один ответ — «войдите заново».

    Здесь же перечитываются роли. Обновление токена по `refresh_token` заодно
    отвечает на вопрос, жив ли человек в Keycloak: `invalid_grant` означает, что
    он вышел или его отключили, и тогда сессия гасится и у нас. Перевод между
    группами доезжает за тот же срок.

    **Недоступный Keycloak сессию не рвёт.** Это разные вещи: «человека
    отключили» и «до Keycloak сейчас не достучаться», и если их смешать, первая
    же сетевая заминка выкинет из системы всех разом — в середине рабочего дня,
    без единой причины в журнале.
    """
    record = await sessions.load_session(db, token)
    if record is None:
        return None
    roles = record.roles
    now = datetime.now(UTC)
    stale_for = (now - record.last_seen_at).total_seconds()
    if record.refresh_token and stale_for >= settings.speech_session_refresh_seconds:
        refreshed = await _refresh_roles(settings, db, record)
        if refreshed is None:
            return None
        roles = refreshed
    elif stale_for >= _TOUCH_MIN_INTERVAL_SECONDS:
        await sessions.touch_session(db, record.id)
        await db.commit()
    if not roles:
        return None
    return Principal(
        sub=record.sub,
        username=record.username,
        email=record.email,
        display_name=record.display_name,
        roles=roles,
        auth_mode="keycloak",
    )


async def _refresh_roles(
    settings: Settings, db: AsyncSession, record: sessions.SessionRecord
) -> tuple[str, ...] | None:
    """Перечитать роли в Keycloak. `None` — сессию держать больше нельзя."""
    from src.infrastructure.auth import speech_oidc as oidc
    from src.infrastructure.auth.keycloak import KeycloakTokenValidator, token_principals

    client = oidc.client_from_settings(settings)
    try:
        tokens = await oidc.refresh_tokens(client, refresh_token=record.refresh_token or "")
    except oidc.OidcError as exc:
        if exc.code == "invalid_grant":
            await sessions.revoke_session(db, record.id)
            await db.commit()
            return None
        return record.roles
    try:
        validator = KeycloakTokenValidator(_SpeechKeycloakSettings(settings))
        claims = await validator.validate_token(str(tokens.get("access_token", "")))
    except Exception:
        return record.roles
    roles = roles_from_groups(
        token_principals(claims.raw, str(settings.speech_keycloak_audience or "")),
        role_aliases(settings),
    )
    await sessions.refresh_session(
        db,
        record.id,
        roles=roles,
        refresh_token=str(tokens.get("refresh_token") or "") or record.refresh_token,
        ttl_seconds=int(settings.speech_session_ttl_seconds),
    )
    await db.commit()
    return roles


def _redirect_uri(settings: Settings) -> str:
    """Адрес возврата — ровно тот, что зарегистрирован в клиенте.

    Собирается из настройки, а не из запроса: за обратным прокси `request.url`
    несёт внутренний адрес, а Keycloak сверяет `redirect_uri` побайтово.
    Расхождение в схеме или порте даёт отказ **после** правильно введённого
    пароля — самый дорогой момент, чтобы узнать об опечатке.
    """
    base = str(settings.speech_public_base_url or "").rstrip("/")
    return f"{base}/api/v1/auth/callback"


class KeycloakIdentityGateway:
    """Проверять личность и управлять сессией в общей сессии запроса."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """Принять сессию транзакции и явную конфигурацию Keycloak."""
        self._session = session
        self._settings = settings

    async def current(self, credentials: Credentials) -> Principal:
        """По cookie затем Bearer вернуть личность с обновлёнными правами."""
        if self._settings.speech_auth_mode.strip().lower() == "open":
            return open_mode_principal()
        if credentials.session_token:
            principal = await principal_from_session(
                self._settings, self._session, credentials.session_token
            )
            if principal is not None:
                return principal
        return await resolve_principal(self._settings, credentials.authorization)

    async def begin_login(self, destination: str) -> BeginLogin:
        """Сформировать адрес OIDC-входа и CSPRNG-параметры одной попытки."""
        settings = self._settings
        if settings.speech_auth_mode.strip().lower() == "open":
            return BeginLogin(destination=destination)
        client = oidc.client_from_settings(settings)
        if not client.is_configured:
            return BeginLogin(destination="/", error="not_configured")
        verifier = oidc.new_pkce_verifier()
        attempt = LoginAttempt(
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(24),
            verifier=verifier,
            next=destination,
        )
        url = oidc.build_authorize_url(
            client,
            redirect_uri=_redirect_uri(settings),
            state=attempt.state,
            nonce=attempt.nonce,
            code_challenge=oidc.pkce_challenge(verifier),
        )
        return BeginLogin(destination=url, attempt=attempt)

    async def logout(self, token: str) -> LogoutResult:
        """Погасить внутреннюю сессию и построить адрес выхода из провайдера."""
        settings = self._settings
        if token:
            await sessions.revoke_session(self._session, sessions.token_hash(token))
            await self._session.commit()
        url = ""
        if settings.speech_auth_mode.strip().lower() != "open":
            client = oidc.client_from_settings(settings)
            if client.is_configured:
                url = oidc.build_logout_url(
                    client,
                    post_logout_redirect_uri=str(settings.speech_public_base_url or "").rstrip("/")
                    or "/",
                )
        return LogoutResult(logout_url=url)

    async def complete_login(self, query: LoginCallbackQuery) -> LoginCompletion:
        """Обменять код на подтверждённую личность и атомарно создать сессию."""
        settings = self._settings
        db = self._session
        if query.attempt is None or query.code is None:
            raise ValueError("callback must be verified before exchanging its code")
        stored = query.attempt.model_dump()
        code = query.code
        client = oidc.client_from_settings(settings)
        try:
            tokens = await oidc.exchange_code(
                client,
                code=code,
                redirect_uri=_redirect_uri(settings),
                code_verifier=stored.get("verifier", ""),
            )
        except oidc.OidcError:
            return LoginCompletion(error="exchange_failed")
        from src.infrastructure.auth.keycloak import (
            KeycloakTokenValidator,
            log_token_refusal,
            token_principals,
        )

        validator = KeycloakTokenValidator(_SpeechKeycloakSettings(settings))
        access_token = str(tokens.get("access_token", ""))
        id_token = str(tokens.get("id_token") or "")
        checked, rejected = ("access_token", access_token)
        try:
            claims = await validator.validate_token(access_token)
            if id_token:
                checked, rejected = ("id_token", id_token)
                id_claims = await validator.validate_token(id_token, access_token=access_token)
                if id_claims.raw.get("nonce") != stored.get("nonce"):
                    return LoginCompletion(error="nonce_mismatch")
        except Exception as exc:
            log_token_refusal(
                logger,
                "speech login",
                checked,
                rejected,
                str(settings.speech_keycloak_audience or ""),
                exc,
            )
            return LoginCompletion(error="token_invalid")
        aliases = role_aliases(settings)
        principals = token_principals(claims.raw, str(settings.speech_keycloak_audience or ""))
        roles = roles_from_groups(principals, aliases)
        if not roles:
            logger.warning(
                "speech login refused: no groups matched; token principals=%s; expected=%s",
                principals,
                sorted({name for names in aliases.values() for name in names}),
            )
            return LoginCompletion(error="no_groups")
        token = sessions.new_session_token()
        await sessions.create_session(
            db,
            token=token,
            sub=claims.keycloak_sub,
            username=claims.username,
            email=claims.email,
            display_name=claims.display_name,
            roles=roles,
            refresh_token=str(tokens.get("refresh_token") or "") or None,
            ttl_seconds=int(settings.speech_session_ttl_seconds),
        )
        await remember_user(
            db,
            Principal(
                sub=claims.keycloak_sub,
                username=claims.username,
                email=claims.email,
                display_name=claims.display_name,
                roles=roles,
                auth_mode="keycloak",
            ),
        )
        await db.commit()
        return LoginCompletion(
            destination=safe_next(stored.get("next")),
            token=token,
            ttl_seconds=int(settings.speech_session_ttl_seconds),
        )
