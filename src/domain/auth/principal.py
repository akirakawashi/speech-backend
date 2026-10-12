"""Роли пользователей речи и вытекающие из них права на операции."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

from src.domain.calls.contracts import SPEECH_ROLE_ADMIN, SPEECH_ROLE_EDITOR, SPEECH_ROLE_READER

_ROLE_ORDER: Final[tuple[str, ...]] = (SPEECH_ROLE_READER, SPEECH_ROLE_EDITOR, SPEECH_ROLE_ADMIN)

PERM_SEARCH: Final[str] = "search"

PERM_DOWNLOAD_AUDIO: Final[str] = "download_audio"

PERM_EXPORT: Final[str] = "export"

PERM_QUALITY: Final[str] = "quality"

PERM_ADMIN: Final[str] = "admin"

_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    SPEECH_ROLE_READER: frozenset({PERM_SEARCH}),
    SPEECH_ROLE_EDITOR: frozenset({PERM_SEARCH, PERM_DOWNLOAD_AUDIO, PERM_EXPORT, PERM_QUALITY}),
    SPEECH_ROLE_ADMIN: frozenset(
        {PERM_SEARCH, PERM_DOWNLOAD_AUDIO, PERM_EXPORT, PERM_QUALITY, PERM_ADMIN}
    ),
}

OPEN_MODE_SUB: Final[str] = "open-admin"


class Principal(BaseModel):
    """Кто выполняет запрос."""

    model_config = ConfigDict(frozen=True)
    sub: str
    username: str | None
    email: str | None
    display_name: str | None
    roles: tuple[str, ...]
    auth_mode: str

    @property
    def permissions(self) -> frozenset[str]:
        """Вернуть разрешённые действия текущей личности."""
        granted: set[str] = set()
        for role in self.roles:
            granted |= _PERMISSIONS.get(role, frozenset())
        return frozenset(granted)

    def can(self, permission: str) -> bool:
        """Проверить, разрешено ли указанное действие ролям личности."""
        return permission in self.permissions


def roles_from_groups(groups: list[str], aliases: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Какие роли даёт набор групп токена.

    Старшая роль добавляет младшие: администратор обязан уметь всё, что умеет
    читатель, и требовать для этого второй группы значит устроить инцидент на
    ровном месте.
    """
    seen = {str(group).strip().lower() for group in groups if str(group).strip()}
    granted: set[str] = set()
    for role, names in aliases.items():
        if any(name.strip().lower() in seen for name in names):
            granted.add(role)
    if not granted:
        return ()
    highest = max(_ROLE_ORDER.index(role) for role in granted if role in _ROLE_ORDER)
    return _ROLE_ORDER[: highest + 1]


def open_mode_principal() -> Principal:
    """Вернуть явно разрешённую локальную личность открытого режима."""
    return Principal(
        sub=OPEN_MODE_SUB,
        username=OPEN_MODE_SUB,
        email=None,
        display_name="Открытый режим",
        roles=_ROLE_ORDER,
        auth_mode="open",
    )
