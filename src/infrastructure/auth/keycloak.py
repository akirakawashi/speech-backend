import asyncio
import base64
import binascii
import json
import logging
import time
from collections.abc import Sequence
from typing import Any, Protocol

import httpx
from jose import JWTError, jwt

from src.application.dto.identity import TokenClaims
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.identity import SUPER_ADMIN_ROLE, map_keycloak_roles

JWKS_CACHE_SECONDS = 300
#: Floor between two fetches of the key set. It collapses the thundering herd on
#: a cold cache, and it bounds what an unknown `kid` can cost: a forged token
#: triggers a refetch (see `_get_key`), so without a floor a flood of them would
#: turn every rejection into an outbound request to Keycloak.
JWKS_MIN_REFETCH_SECONDS = 10


class KeycloakConfiguration(Protocol):
    """Проверенный набор параметров валидатора без чтения окружения."""

    @property
    def keycloak_issuer_url(self) -> str:
        """Вернуть допустимый issuer."""
        ...

    @property
    def keycloak_jwks_url(self) -> str:
        """Вернуть адрес открытых ключей."""
        ...

    @property
    def keycloak_audience(self) -> str:
        """Вернуть допустимую аудиторию."""
        ...

    @property
    def keycloak_role_aliases_json(self) -> dict[str, list[str]]:
        """Вернуть проверенное сопоставление групп ролям."""
        ...

    @property
    def keycloak_super_admin_emails(self) -> Sequence[str]:
        """Вернуть явно разрешённые адреса администраторов."""
        ...


def unauthorized(detail: str = "Invalid authentication credentials") -> ApplicationError:
    """Создать нормализованный отказ аутентификации без раскрытия токена."""
    return ApplicationError(
        kind=ErrorKind.UNAUTHORIZED,
        detail=detail,
        challenge=True,
    )


def token_principals(claims: dict[str, Any], audience: str) -> list[str]:
    """Сырые имена из токена: роли realm, роли клиента и группы.

    Отдельная функция, а не метод, потому что нужна **до** отображения ролей.
    `TokenClaims.roles` — уже результат: `map_keycloak_roles` оставляет там
    только то, что нашлось в таблице псевдонимов, и всё остальное молча
    выбрасывает. Сервису с собственной таблицей групп (Speech Analytics) из
    этого списка взять нечего: его группы `whisper-icg-*` в таблице платформы
    отсутствуют, то есть исчезают по дороге. Ролей не получал бы никто — и
    выглядело бы это как «человеку не выдали группу».
    """
    principals: set[str] = set()

    realm_access = claims.get("realm_access")
    if isinstance(realm_access, dict):
        principals.update(_string_values(realm_access.get("roles")))

    resource_access = claims.get("resource_access")
    if isinstance(resource_access, dict):
        client_access = resource_access.get(audience)
        if isinstance(client_access, dict):
            principals.update(_string_values(client_access.get("roles")))

    principals.update(_string_values(claims.get("groups")))

    return sorted(principals)


def _string_values(value: object) -> list[str]:
    """Выделить непустые строковые значения из поля провайдера."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def describe_token(token: str) -> str:
    """Конверт токена словами — без проверки подписи.

    Это не доверие к содержимому, а описание того, что пришло: подпись как раз
    и не сошлась. Рядом с причиной отказа нужен именно конверт — алгоритм,
    `kid`, издатель и аудитория: частые причины (чужой алгоритм, шифрованный
    токен, несовпавшая аудитория) отличаются только здесь, а по сообщению
    `jose` неразличимы.
    """
    parts = token.split(".")
    if len(parts) == 5:
        # Пять частей — это JWE: токен зашифрован, и `jwt.decode` с RS256 до
        # содержимого не доберётся никогда. Случай лечится настройкой клиента,
        # а не нашим кодом, поэтому назван отдельно.
        return "shape=encrypted(JWE)"
    if len(parts) != 3:
        return f"shape=unusable(parts={len(parts)}, chars={len(token)})"
    header = _peek(parts[0])
    payload = _peek(parts[1])
    return (
        f"alg={header.get('alg')} kid={str(header.get('kid', ''))[:8]} "
        f"iss={payload.get('iss')} aud={payload.get('aud')} azp={payload.get('azp')}"
    )


def log_token_refusal(
    log: logging.Logger,
    context: str,
    kind: str,
    token: str,
    expected_audience: str,
    exc: BaseException,
) -> None:
    """Записать, почему токен не прошёл проверку.

    Наружу уходит одно слово — `token_invalid`; человеку незачем знать, подпись
    это, аудитория или срок. Но когда причина не остаётся и в журнале, разбор
    отказа превращается в перебор версий: токен живёт минуты, второй раз его не
    выдадут, а воспроизвести вход посторонними средствами нельзя — клиент
    конфиденциальный. Однажды это уже стоило дня и лишней выкатки образа.

    В запись идут только свойства конверта. Ни токена, ни имени, ни почты: по
    ним причину всё равно не узнать, а журнал живёт дольше сессии.
    """
    cause = exc.__cause__
    log.warning(
        "%s refused: %s rejected: %s: %s%s; expected aud=%s; %s",
        context,
        kind,
        type(exc).__name__,
        getattr(exc, "detail", None) or str(exc) or type(exc).__name__,
        f" <- {type(cause).__name__}: {cause}" if cause is not None else "",
        expected_audience,
        describe_token(token),
    )


def _peek(segment: str) -> dict[str, Any]:
    """Разобрать часть токена «на посмотреть». Непонятная часть — пустой ответ.

    Отказ здесь недопустим: это путь отказа, и уронить его разбором значит
    заменить объяснимое «не пустили» на 500 без причины.
    """
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        parsed = json.loads(raw)
    except (ValueError, binascii.Error):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class KeycloakTokenValidator:
    """Проверить подпись, срок и назначение JWT по ключам Keycloak."""

    def __init__(self, settings: KeycloakConfiguration) -> None:
        """Настроить KeycloakTokenValidator из переданных параметров и зависимостей."""
        self._settings = settings
        self._jwks: dict[str, Any] | None = None
        self._jwks_expires_at = 0.0
        self._jwks_min_refetch_at = 0.0
        self._jwks_lock = asyncio.Lock()

    async def validate_token(self, token: str, *, access_token: str | None = None) -> TokenClaims:
        """Проверить токен Keycloak.

        `access_token` передаётся, когда проверяется **ID-токен**, полученный
        вместе с ним: в ID-токене кода-флоу Keycloak всегда есть `at_hash` —
        отпечаток того access-токена, с которым он выдан. `jose` сверяет клейм
        сам, но без этого значения отказывает: «No access_token provided to
        compare against at_hash claim». Отключать проверку было бы проще и
        неверно — `at_hash` затем и существует, чтобы подставленный ID-токен
        нельзя было предъявить вместе с чужим access-токеном.

        Для Bearer-токенов в API параметр не нужен: `at_hash` в них не бывает,
        и `jose` эту проверку пропускает.
        """
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if not kid:
                raise unauthorized("Token header does not contain kid")

            key = await self._get_key(str(kid))
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self._settings.keycloak_issuer_url,
                access_token=access_token,
                options={"verify_aud": False},
            )
            self._validate_audience(claims)
        except ApplicationError:
            raise
        except JWTError as exc:
            raise unauthorized("Token validation failed") from exc

        return self._extract_claims(claims)

    def _validate_audience(self, claims: dict[str, Any]) -> None:
        """Проверить назначение JWT относительно ожидаемого клиента."""
        expected = self._settings.keycloak_audience
        aud = claims.get("aud")
        audiences = set(
            self._string_list(aud)
            if isinstance(aud, list)
            else [aud]
            if isinstance(aud, str)
            else []
        )
        azp = claims.get("azp")
        if isinstance(azp, str):
            audiences.add(azp)

        if expected not in audiences:
            raise unauthorized("Token validation failed")

    async def _get_key(self, kid: str) -> dict[str, Any]:
        """Получить ключ подписи токена с обновлением кэша при неизвестном kid."""
        key = self._find_key(await self._get_jwks(), kid)
        if key is not None:
            return key

        # An unknown `kid` is either a forged token or a key rotation this
        # process has not seen yet, and the cheap read cannot tell them apart.
        # Without this refetch the second case rejects every genuine token
        # signed by the new key until the cache expires -- a self-inflicted
        # outage of up to JWKS_CACHE_SECONDS on every rotation. The refetch is
        # floored (JWKS_MIN_REFETCH_SECONDS) so the first case cannot be used
        # to drive load at Keycloak.
        key = self._find_key(await self._refresh_jwks(), kid)
        if key is not None:
            return key

        raise unauthorized("Token signing key was not found")

    @staticmethod
    def _find_key(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
        """Найти ключ с указанным kid в наборе ключей провайдера."""
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            return None
        for key in keys:
            if isinstance(key, dict) and key.get("kid") == kid:
                return key
        return None

    async def _get_jwks(self) -> dict[str, Any]:
        """Вернуть актуальные ключи подписи, обновив истёкший кэш."""
        if self._jwks is not None and time.monotonic() < self._jwks_expires_at:
            return self._jwks
        return await self._refresh_jwks()

    async def _refresh_jwks(self) -> dict[str, Any]:
        """Обновить кэш ключей подписи по ответу Keycloak."""
        async with self._jwks_lock:
            # Re-checked while holding the lock: concurrent callers on a cold
            # cache would otherwise each open their own request to Keycloak.
            if self._jwks is not None and time.monotonic() < self._jwks_min_refetch_at:
                return self._jwks

            jwks = await self._fetch_jwks()
            now = time.monotonic()
            self._jwks = jwks
            self._jwks_expires_at = now + JWKS_CACHE_SECONDS
            self._jwks_min_refetch_at = now + JWKS_MIN_REFETCH_SECONDS
            return jwks

    async def _fetch_jwks(self) -> dict[str, Any]:
        # An unreachable identity provider is not a bad credential. Letting the
        # httpx error escape turned it into a 500, which tells the caller their
        # request was broken; 503 says the dependency is down and the same
        # request is worth retrying.
        """Запросить ключи подписи Keycloak и вернуть проверенное содержимое ответа."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self._settings.keycloak_jwks_url)
                response.raise_for_status()
            jwks = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ApplicationError(
                kind=ErrorKind.UNAVAILABLE,
                detail="Identity provider is unavailable",
            ) from exc

        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise ApplicationError(
                kind=ErrorKind.UNAVAILABLE,
                detail="Identity provider returned an unusable key set",
            )
        return jwks

    def _extract_claims(self, claims: dict[str, Any]) -> TokenClaims:
        """Собрать типизированную личность из проверенного содержимого JWT."""
        subject = self._required_str(claims, "sub")
        username = self._optional_str(claims, "preferred_username")
        email = self._optional_str(claims, "email")
        display_name = self._optional_str(claims, "name")
        employee_guid = (
            self._optional_str(claims, "employeeGuid")
            or self._optional_str(claims, "employee_guid")
            or subject
        )

        roles = set(
            map_keycloak_roles(
                self._extract_raw_principals(claims),
                self._settings.keycloak_role_aliases_json,
            )
        )
        if self._is_configured_super_admin_email(email):
            roles.add(SUPER_ADMIN_ROLE)

        return TokenClaims(
            keycloak_sub=subject,
            employee_guid=employee_guid,
            username=username or email or display_name or subject,
            email=email,
            display_name=display_name,
            roles=sorted(roles),
            raw=claims,
            mfa=self._has_mfa(claims),
        )

    def _is_configured_super_admin_email(self, email: str | None) -> bool:
        """Проверить совпадение email с явно разрешёнными администраторами."""
        if not email:
            return False
        return email.strip().lower() in set(self._settings.keycloak_super_admin_emails)

    def _extract_raw_principals(self, claims: dict[str, Any]) -> list[str]:
        """Прочитать группы и роли из полей JWT провайдера."""
        return token_principals(claims, self._settings.keycloak_audience)

    def _has_mfa(self, claims: dict[str, Any]) -> bool:
        """Определить, сообщает ли JWT о дополнительном факторе входа."""
        amr = {item.lower() for item in self._string_list(claims.get("amr"))}
        if amr.intersection({"mfa", "otp", "totp", "webauthn", "fido", "fido2", "smartcard"}):
            return True

        acr = self._optional_str(claims, "acr")
        if not acr:
            return False
        normalized = acr.lower()
        return any(
            marker in normalized for marker in ("mfa", "2fa", "loa2", "loa3", "aal2", "aal3")
        )

    def _required_str(self, claims: dict[str, Any], key: str) -> str:
        """Прочитать обязательную непустую строку либо отклонить ответ провайдера."""
        value = claims.get(key)
        if not isinstance(value, str) or not value:
            raise unauthorized(f"Token claim '{key}' is missing")
        return value

    def _optional_str(self, claims: dict[str, Any], key: str) -> str | None:
        """Прочитать необязательную непустую строку; отсутствие вернуть как None."""
        value = claims.get(key)
        return value if isinstance(value, str) and value else None

    def _string_list(self, value: object) -> list[str]:
        """Прочитать список строк из значения JWT."""
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item]
