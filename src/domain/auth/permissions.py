MANAGE_AGENT_GROUPS = "manage_agent_groups"
DETAIL_SETTINGS = "detail_settings"

SUPER_ADMIN_ROLE = "super_admin"
ADMIN_ROLE = "admin"

#: Базовый доступ: войти и пользоваться агентами, без администрирования.
#:
#: Роль заведена не ради прав — прав она не даёт никаких, — а ради **входа**.
#: До 2026-09-17 «ни одной группы» означало «обычный пользователь», и человек
#: без единой нашей группы входил и работал наравне со всеми. Теперь пустой
#: список ролей означает «доступ не выдан», и базовому пользователю нужно имя,
#: иначе его нечем отличить от постороннего.
USER_ROLE = "user"

MANAGER_ROLES = {SUPER_ADMIN_ROLE, ADMIN_ROLE}

#: Машинное чтение выгрузки в корпоративный DWH (`/api/v1/dwh`).
#:
#: Роль клиента, а не человека, и входа в продукт она не даёт: токен, у
#: которого нет других ролей, получает `403` на любом маршруте, кроме
#: выгрузки. Иначе клиент DWH, выданный для чтения метрик, мог бы
#: разговаривать с агентами и стал бы строкой в `users` — «новым
#: пользователем» в тех самых метриках, которые он читает.
ANALYTICS_READER_ROLE = "analytics_reader"

#: Роли, которые сами по себе не открывают продукт.
MACHINE_ONLY_ROLES = frozenset({ANALYTICS_READER_ROLE})


def grants_product_access(roles: list[str]) -> bool:
    """Есть ли у принципала роль, открывающая продукт, а не только выгрузку."""
    return bool(set(roles) - MACHINE_ONLY_ROLES)


def permissions_for_roles(roles: list[str]) -> list[str]:
    """Объединить права переданных ролей в неизменяемый набор."""
    role_set = set(roles)
    permissions: list[str] = []

    if role_set.intersection(MANAGER_ROLES):
        permissions.append(MANAGE_AGENT_GROUPS)
    if SUPER_ADMIN_ROLE in role_set:
        permissions.append(DETAIL_SETTINGS)

    return permissions


def can_manage_agent_groups(roles: list[str]) -> bool:
    """Проверить разрешение на управление группами доступа."""
    return MANAGE_AGENT_GROUPS in permissions_for_roles(roles)


def is_agent_access_admin(roles: list[str]) -> bool:
    """Проверить право управления доступом к агентам."""
    return bool(set(roles).intersection(MANAGER_ROLES))
