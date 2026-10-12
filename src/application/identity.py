"""Применение правил ролей к проверенным claims внешнего провайдера."""

from src.domain.auth.permissions import SUPER_ADMIN_ROLE as SUPER_ADMIN_ROLE
from src.domain.auth.role_mapping import map_keycloak_roles as map_keycloak_roles
