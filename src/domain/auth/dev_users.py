from pydantic import BaseModel, ConfigDict

from src.domain.auth.identity import TokenClaims
from src.domain.auth.permissions import ADMIN_ROLE, SUPER_ADMIN_ROLE

ROLE_HR_TECH = "group:hr_tech"
ROLE_TECH_ECONOMY = "group:tech_economy"
ROLE_TECH_ONLY = "group:tech_only"

AGENT_ACCESS_GROUPS = [
    {
        "id": ROLE_HR_TECH,
        "name": "HR + Tech support",
        "description": "Users who can use HR agent and tech support agent.",
    },
    {
        "id": ROLE_TECH_ECONOMY,
        "name": "Tech support + Economic helper",
        "description": "Users who can use tech support and economic helper agents.",
    },
    {
        "id": ROLE_TECH_ONLY,
        "name": "Tech support only",
        "description": "Users who can use only the tech support agent.",
    },
]


class DevUser(BaseModel):
    """Локальная тестовая личность для проверок правил доступа."""

    model_config = ConfigDict(frozen=True)

    id: str
    username: str
    display_name: str
    email: str
    roles: list[str]
    description: str


DEV_USERS = [
    DevUser(
        id="super-admin",
        username="super.admin",
        display_name="Super Admin",
        email="super.admin@ic-group.local",
        roles=[SUPER_ADMIN_ROLE],
        description="Full access, including detailed settings.",
    ),
    DevUser(
        id="admin",
        username="admin",
        display_name="Admin",
        email="admin@ic-group.local",
        roles=[ADMIN_ROLE],
        description="All operational access except detailed settings.",
    ),
    DevUser(
        id="hr-tech",
        username="hr.tech",
        display_name="HR + Tech User",
        email="hr.tech@ic-group.local",
        roles=[ROLE_HR_TECH],
        description="Access to HR agent and tech support agent.",
    ),
    DevUser(
        id="employee-basic",
        username="employee.basic",
        display_name="Basic Employee User",
        email="employee.basic@ic-group.local",
        roles=[],
        description="Authenticated employee without Aicistent access groups.",
    ),
    DevUser(
        id="tech-economy",
        username="tech.economy",
        display_name="Tech + Economy User",
        email="tech.economy@ic-group.local",
        roles=[ROLE_TECH_ECONOMY],
        description="Access to tech support and economic helper agents.",
    ),
    DevUser(
        id="tech-only",
        username="tech.only",
        display_name="Tech Support User",
        email="tech.only@ic-group.local",
        roles=[ROLE_TECH_ONLY],
        description="Access only to tech support agent.",
    ),
]

DEV_USERS_BY_ID = {user.id: user for user in DEV_USERS}


def list_dev_users() -> list[DevUser]:
    """Вернуть разрешённые локальные тестовые личности."""
    return DEV_USERS


def get_dev_user(user_id: str | None) -> DevUser | None:
    """Найти локальную тестовую личность по имени."""
    if not user_id:
        return None
    return DEV_USERS_BY_ID.get(user_id)


def claims_for_dev_user(user: DevUser) -> TokenClaims:
    """Построить проверенную локальную личность из выбранного тестового пользователя."""
    return TokenClaims(
        keycloak_sub=f"dev:{user.id}",
        employee_guid=f"dev:{user.id}",
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        roles=user.roles,
        raw={
            "sub": f"dev:{user.id}",
            "preferred_username": user.username,
            "name": user.display_name,
            "email": user.email,
            "roles": user.roles,
            "dev_auth": True,
        },
        mfa=True,
    )
