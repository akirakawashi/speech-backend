import uuid

from pydantic import BaseModel, ConfigDict


class TokenClaims(BaseModel):
    """Проверенные сведения JWT без привязки бизнес-правил к HTTP."""

    model_config = ConfigDict(frozen=True)

    keycloak_sub: str
    employee_guid: str
    username: str | None
    email: str | None
    display_name: str | None
    roles: list[str]
    raw: dict[str, object]
    mfa: bool = False


class CurrentUser(BaseModel):
    """Проверенная личность и её роли для применения прав доступа."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    keycloak_sub: str
    employee_guid: str | None
    username: str | None
    email: str | None
    display_name: str | None
    roles: list[str]
    mfa: bool = False
