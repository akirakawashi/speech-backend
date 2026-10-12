"""Порт проверки внешней личности и управления серверной сессией."""

from typing import Protocol

from src.application.authorization import Principal
from src.application.dto.authentication import (
    BeginLogin,
    Credentials,
    LoginCallbackQuery,
    LoginCompletion,
    LogoutResult,
)


class IdentityGateway(Protocol):
    """Операции, необходимые сценариям входа и текущей личности."""

    async def current(self, credentials: Credentials) -> Principal:
        """Проверить сессию или токен и вернуть личность; при отказе выдать ошибку порта."""
        ...

    async def begin_login(self, destination: str) -> BeginLogin:
        """Создать связанный запрос входа и вернуть его адрес и параметры."""
        ...

    async def complete_login(self, query: LoginCallbackQuery) -> LoginCompletion:
        """Обменять проверенный возврат на сессию, атомарно сохранить пользователя."""
        ...

    async def logout(self, token: str) -> LogoutResult:
        """Погасить сессию и вернуть адрес внешнего выхода, если он настроен."""
        ...
