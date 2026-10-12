"""Права на сценарии для любой точки входа приложения."""

from src.application.errors.requests import ApplicationError, ErrorKind
from src.domain.auth.principal import (
    _PERMISSIONS as _PERMISSIONS,
    _ROLE_ORDER as _ROLE_ORDER,
    OPEN_MODE_SUB as OPEN_MODE_SUB,
    PERM_ADMIN as PERM_ADMIN,
    PERM_DOWNLOAD_AUDIO as PERM_DOWNLOAD_AUDIO,
    PERM_EXPORT as PERM_EXPORT,
    PERM_QUALITY as PERM_QUALITY,
    PERM_SEARCH as PERM_SEARCH,
    Principal as Principal,
    open_mode_principal as open_mode_principal,
    roles_from_groups as roles_from_groups,
)


def ensure_permission(principal: Principal, permission: str) -> None:
    """По личности и праву разрешить сценарий или вернуть отказ доступа."""
    if not principal.can(permission):
        raise ApplicationError(ErrorKind.FORBIDDEN, "forbidden")
