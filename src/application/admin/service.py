"""Сценарии admin: проверка доступа и вызов внедрённых операций."""

from __future__ import annotations

from src.application.authorization import (
    PERM_ADMIN,
    ensure_permission,
)
from src.application.dto.admin_operations import (
    ActionResponse,
    AuditResponse,
    ClassifierAdminResponse,
    ClassifierNodeRow,
    JobsResponse,
    ListClassifierQuery,
    ListJobsQuery,
    ListProjectsQuery,
    ListUsersQuery,
    PauseBackfillQuery,
    PipelineQuery,
    PipelineResponse,
    ProjectRow,
    ProjectsAdminResponse,
    ReadAuditQuery,
    ReadSettingsQuery,
    ReprocessCallQuery,
    RetryJobQuery,
    SettingsResponse,
    StartBackfillQuery,
    SyncDirectoriesQuery,
    UpdateClassifierNodeQuery,
    UpdateProjectQuery,
    UsersResponse,
    WriteSettingsQuery,
)
from src.application.dto.calls import SPEECH_DEFAULT_SETTINGS
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.admin_operations import AdminRepository
from src.application.runtime_settings import validated_setting


class AdminService:
    """Выполнять сценарии admin через заменяемый порт."""

    def __init__(self, repository: AdminRepository) -> None:
        """Принять реализацию внешних операций; не создавать ресурсы самостоятельно."""
        self._repository = repository

    async def list_classifier(self, query: ListClassifierQuery) -> ClassifierAdminResponse:
        """Вернуть административный снимок дерева; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.list_classifier(query)

    async def update_classifier_node(self, query: UpdateClassifierNodeQuery) -> ClassifierNodeRow:
        """Изменить доступность узла из DTO, сохранив историю; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.update_classifier_node(query)

    async def list_projects(self, query: ListProjectsQuery) -> ProjectsAdminResponse:
        """Вернуть проекты с правилами приёма и разметки; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.list_projects(query)

    async def update_project(self, query: UpdateProjectQuery) -> ProjectRow:
        """Сохранить настройки проекта из DTO и вернуть его состояние; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.update_project(query)

    async def read_settings(self, query: ReadSettingsQuery) -> SettingsResponse:
        """Вернуть действующие настройки и переопределённые ключи; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.read_settings(query)

    async def write_settings(self, query: WriteSettingsQuery) -> SettingsResponse:
        """Проверить весь патч DTO, записать значения и вернуть настройки; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        unknown = sorted(set(query.payload.values) - set(SPEECH_DEFAULT_SETTINGS))
        if unknown:
            raise ApplicationError(
                ErrorKind.INVALID_INPUT, f"unknown_settings: {', '.join(unknown)}"
            )
        try:
            checked = {
                key: validated_setting(key, value) for key, value in query.payload.values.items()
            }
        except ValueError as exc:
            raise ApplicationError(ErrorKind.INVALID_INPUT, str(exc)) from exc
        payload = query.payload.model_copy(update={"values": checked})
        return await self._repository.write_settings(query.model_copy(update={"payload": payload}))

    async def read_audit(self, query: ReadAuditQuery) -> AuditResponse:
        """По фильтрам DTO вернуть страницу журнала действий; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.read_audit(query)

    async def list_users(self, query: ListUsersQuery) -> UsersResponse:
        """По фильтрам DTO вернуть пользователей с наблюдавшимися ролями; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.list_users(query)

    async def pipeline(self, query: PipelineQuery) -> PipelineResponse:
        """Вернуть состояние полос конвейера и зависимостей; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.pipeline(query)

    async def list_jobs(self, query: ListJobsQuery) -> JobsResponse:
        """По фильтрам DTO вернуть страницу заданий очереди; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.list_jobs(query)

    async def retry_job(self, query: RetryJobQuery) -> ActionResponse:
        """По UUID повторить неудавшееся задание и вернуть итог действия; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.retry_job(query)

    async def reprocess_call(self, query: ReprocessCallQuery) -> ActionResponse:
        """По UUID поставить звонок на обработку с указанного этапа; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.reprocess_call(query)

    async def sync_directories(self, query: SyncDirectoriesQuery) -> ActionResponse:
        """Согласовать сотрудников и проекты с источником; вернуть сводку, нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.sync_directories(query)

    async def pause_backfill(self, query: PauseBackfillQuery) -> ActionResponse:
        """Приостановить историю и вернуть новое состояние; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.pause_backfill(query)

    async def start_backfill(self, query: StartBackfillQuery) -> ActionResponse:
        """Возобновить историю и вернуть новое состояние; нужно право admin."""
        ensure_permission(query.actor, PERM_ADMIN)
        return await self._repository.start_backfill(query)
