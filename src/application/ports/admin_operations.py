"""Порт операций admin без технических типов реализации."""

from typing import Protocol

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


class AdminRepository(Protocol):
    """Внешние операции, необходимые сценариям admin."""

    async def list_classifier(self, query: ListClassifierQuery) -> ClassifierAdminResponse:
        """Выполнить операцию list_classifier и вернуть её типизированную проекцию."""
        ...

    async def update_classifier_node(self, query: UpdateClassifierNodeQuery) -> ClassifierNodeRow:
        """Выполнить операцию update_classifier_node и вернуть её типизированную проекцию."""
        ...

    async def list_projects(self, query: ListProjectsQuery) -> ProjectsAdminResponse:
        """Выполнить операцию list_projects и вернуть её типизированную проекцию."""
        ...

    async def update_project(self, query: UpdateProjectQuery) -> ProjectRow:
        """Выполнить операцию update_project и вернуть её типизированную проекцию."""
        ...

    async def read_settings(self, query: ReadSettingsQuery) -> SettingsResponse:
        """Выполнить операцию read_settings и вернуть её типизированную проекцию."""
        ...

    async def write_settings(self, query: WriteSettingsQuery) -> SettingsResponse:
        """Выполнить операцию write_settings и вернуть её типизированную проекцию."""
        ...

    async def read_audit(self, query: ReadAuditQuery) -> AuditResponse:
        """Выполнить операцию read_audit и вернуть её типизированную проекцию."""
        ...

    async def list_users(self, query: ListUsersQuery) -> UsersResponse:
        """Выполнить операцию list_users и вернуть её типизированную проекцию."""
        ...

    async def pipeline(self, query: PipelineQuery) -> PipelineResponse:
        """Выполнить операцию pipeline и вернуть её типизированную проекцию."""
        ...

    async def list_jobs(self, query: ListJobsQuery) -> JobsResponse:
        """Выполнить операцию list_jobs и вернуть её типизированную проекцию."""
        ...

    async def retry_job(self, query: RetryJobQuery) -> ActionResponse:
        """Выполнить операцию retry_job и вернуть её типизированную проекцию."""
        ...

    async def reprocess_call(self, query: ReprocessCallQuery) -> ActionResponse:
        """Выполнить операцию reprocess_call и вернуть её типизированную проекцию."""
        ...

    async def sync_directories(self, query: SyncDirectoriesQuery) -> ActionResponse:
        """Выполнить операцию sync_directories и вернуть её типизированную проекцию."""
        ...

    async def pause_backfill(self, query: PauseBackfillQuery) -> ActionResponse:
        """Выполнить операцию pause_backfill и вернуть её типизированную проекцию."""
        ...

    async def start_backfill(self, query: StartBackfillQuery) -> ActionResponse:
        """Выполнить операцию start_backfill и вернуть её типизированную проекцию."""
        ...
