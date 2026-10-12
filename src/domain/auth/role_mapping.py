from src.domain.auth.dev_users import ROLE_HR_TECH, ROLE_TECH_ECONOMY, ROLE_TECH_ONLY
from src.domain.auth.permissions import (
    ADMIN_ROLE,
    ANALYTICS_READER_ROLE,
    SUPER_ADMIN_ROLE,
    USER_ROLE,
)

RoleAliasConfig = dict[str, list[str]]

#: Группы заказчика живут здесь, а не только в `KEYCLOAK_ROLE_ALIASES_JSON`.
#:
#: Переопределение через переменную окружения остаётся — им подстраиваются под
#: переименование группы, — но доступ не должен держаться на файле `.env`
#: единственного узла: переустановят файл, и права исчезнут молча, а выглядеть
#: это будет как «у всех пропал доступ после обновления».
DEFAULT_KEYCLOAK_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    SUPER_ADMIN_ROLE: (
        SUPER_ADMIN_ROLE,
        "super-admin",
        "aicistent_super_admin",
        "aicistent-super-admin",
        "aicistent:super_admin",
        "/aicistent/super_admin",
        "/aicistent/super-admin",
        "LLM-icg-admins",
        "/LLM-icg-admins",
    ),
    USER_ROLE: (
        USER_ROLE,
        "LLM-icg-readers",
        "/LLM-icg-readers",
    ),
    ADMIN_ROLE: (
        ADMIN_ROLE,
        "aicistent_admin",
        "aicistent-admin",
        "aicistent:admin",
        "/aicistent/admin",
    ),
    ROLE_HR_TECH: (
        ROLE_HR_TECH,
        "hr_tech",
        "hr-tech",
        "aicistent_hr_tech",
        "aicistent-hr-tech",
        "/aicistent/hr_tech",
        "/aicistent/hr-tech",
    ),
    ROLE_TECH_ECONOMY: (
        ROLE_TECH_ECONOMY,
        "tech_economy",
        "tech-economy",
        "aicistent_tech_economy",
        "aicistent-tech-economy",
        "/aicistent/tech_economy",
        "/aicistent/tech-economy",
    ),
    ROLE_TECH_ONLY: (
        ROLE_TECH_ONLY,
        "tech_only",
        "tech-only",
        "aicistent_tech_only",
        "aicistent-tech-only",
        "/aicistent/tech_only",
        "/aicistent/tech-only",
    ),
    # Клиент выгрузки в DWH (client credentials, клиент `ai-dwh`). Роль или
    # группа сервисной учётной записи — как её назовёт администратор SSO.
    ANALYTICS_READER_ROLE: (
        ANALYTICS_READER_ROLE,
        "analytics-reader",
        "aicistent_analytics_reader",
        "aicistent-analytics-reader",
        "/aicistent/analytics_reader",
        "/aicistent/analytics-reader",
    ),
}


def map_keycloak_roles(
    raw_principals: list[str], configured_aliases: RoleAliasConfig | None = None
) -> list[str]:
    """Сопоставить проверенные группы Keycloak с разрешёнными ролями."""
    alias_map = _build_alias_map(configured_aliases or {})
    mapped_roles: set[str] = set()

    for principal in raw_principals:
        role = alias_map.get(_normalize_alias(principal))
        if role:
            mapped_roles.add(role)

    return sorted(mapped_roles)


def _build_alias_map(configured_aliases: RoleAliasConfig) -> dict[str, str]:
    """Построить нормализованное соответствие внешних групп ролям."""
    alias_map: dict[str, str] = {}

    for role, aliases in DEFAULT_KEYCLOAK_ROLE_ALIASES.items():
        for alias in aliases:
            alias_map[_normalize_alias(alias)] = role

    for role, configured_alias_list in configured_aliases.items():
        for alias in configured_alias_list:
            alias_map[_normalize_alias(alias)] = role

    return alias_map


def _normalize_alias(value: str) -> str:
    """Привести имя группы или роли к виду для сопоставления."""
    return value.strip().replace("\\", "/").lower()
