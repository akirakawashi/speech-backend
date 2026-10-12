"""Сценарии текущей личности, входа и выхода."""

import secrets

from src.application.authorization import Principal
from src.application.dto.authentication import (
    BeginLogin,
    CookieSettings,
    Credentials,
    LoginCallbackQuery,
    LoginCompletion,
    LogoutResult,
)
from src.application.ports.authentication import IdentityGateway
from src.domain.auth.navigation import safe_next as safe_next


class AuthenticationService:
    """Связать ответ входа с исходной попыткой до обращения к внешнему провайдеру."""

    def __init__(
        self, gateway: IdentityGateway, cookies: CookieSettings, *, open_mode: bool
    ) -> None:
        """Принять порт личности, публичные параметры cookies и режим входа."""
        self._gateway = gateway
        self.cookies = cookies
        self._open = open_mode

    async def current(self, credentials: Credentials) -> Principal:
        """По доставленным учётным данным вернуть подтверждённую личность."""
        return await self._gateway.current(credentials)

    async def begin_login(self, destination: str | None) -> BeginLogin:
        """Проверить адрес возврата и создать попытку входа либо вернуть открытый режим."""
        return await self._gateway.begin_login(safe_next(destination))

    async def complete_login(self, query: LoginCallbackQuery) -> LoginCompletion:
        """Проверить связь state и наличие кода до обмена и создания сессии."""
        if self._open:
            return LoginCompletion()
        if query.error:
            return LoginCompletion(error="provider_error")
        attempt = query.attempt
        if (
            attempt is None
            or not attempt.state
            or not query.state
            or not secrets.compare_digest(attempt.state, query.state)
        ):
            return LoginCompletion(error="state_mismatch")
        if not query.code:
            return LoginCompletion(error="no_code")
        return await self._gateway.complete_login(query)

    async def logout(self, token: str) -> LogoutResult:
        """Погасить даже просроченную сессию без требования текущей авторизации."""
        return await self._gateway.logout(token)
