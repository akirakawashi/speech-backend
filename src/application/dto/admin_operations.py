"""Типизированные команды и проекции операций admin."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from src.application.authorization import Principal
from src.application.dto.api import Envelope
from src.application.dto.calls import (
    ClassifierSource,
    JobKind,
    JobLane,
    JobStatus,
    Product,
    Segment,
)


class ClassifierNodeRow(BaseModel):
    """Узел классификатора обращений (SP-310)."""

    id: uuid.UUID
    parent_id: uuid.UUID | None = None
    level: int
    label: str
    source: ClassifierSource
    sort_order: int
    is_active: bool
    first_seen_at: datetime
    operator_calls: int = 0


class ClassifierAdminResponse(Envelope):
    """Плоское дерево классификатора для административного экрана."""

    items: list[ClassifierNodeRow] = Field(default_factory=list)


class ClassifierNodePatch(BaseModel):
    """Изменение доступности узла без удаления его истории."""

    is_active: bool


class ProjectPatch(BaseModel):
    """Что администратор может менять у очереди NCC.

    `None` означает «не трогать», а не «очистить»: половина полей формы приходит
    незаполненной, и трактовать это как стирание значило бы терять сопоставление
    при каждой правке названия.
    """

    ingest_enabled: bool | None = None
    segment: Segment | None = None
    product: Product | None = None
    clear_segment: bool = False
    clear_product: bool = False


class ProjectRow(BaseModel):
    """Проект источника, сопоставление и разрешение на приём звонков."""

    id: uuid.UUID
    ncc_uuid: str | None = None
    source_key: str | None = None
    title: str
    data_channel: str | None = None
    direction: str
    segment: str | None = None
    product: str | None = None
    ingest_enabled: bool
    last_call_at: datetime | None = None
    synced_at: datetime | None = None
    calls: int = 0


class ProjectsAdminResponse(Envelope):
    """Известные проекты источника для административного экрана."""

    items: list[ProjectRow] = Field(default_factory=list)


class SettingsResponse(Envelope):
    """Значения администраторских настроек и список переопределённых ключей."""

    values: dict[str, JsonValue] = Field(
        default_factory=dict, json_schema_extra={"additionalProperties": True}
    )
    overridden: list[str] = Field(default_factory=list)


class SettingsPatch(BaseModel):
    """Запрошенные изменения администраторских настроек."""

    values: dict[str, JsonValue] = Field(json_schema_extra={"additionalProperties": True})


class LaneRow(BaseModel):
    """Число заданий одного состояния и полосы для панели конвейера."""

    lane: str
    status: str
    count: int
    oldest_seconds: float | None = None


class DependencyRow(BaseModel):
    """Результат проверки доступности одной внешней зависимости."""

    name: str
    configured: bool
    reachable: bool | None = None
    detail: str | None = None


class PipelineResponse(Envelope):
    """Состояние очереди, ошибки и доступность зависимостей."""

    lanes: list[LaneRow] = Field(default_factory=list)
    dead_total: int = 0
    failed_last_24h: int = 0
    errors_by_class: dict[str, int] = Field(default_factory=dict)
    dependencies: list[DependencyRow] = Field(default_factory=list)


class JobRow(BaseModel):
    """Задание конвейера и метаданные его выполнения для администратора."""

    id: uuid.UUID
    call_id: uuid.UUID
    session_id: str
    kind: str
    lane: str
    status: str
    attempts: int
    max_attempts: int
    last_error_class: str | None = None
    last_error_at: datetime | None = None
    created_at: datetime
    finished_at: datetime | None = None


class JobsResponse(Envelope):
    """Страница заданий и число заданий, подходящих под отбор."""

    items: list[JobRow] = Field(default_factory=list)
    total: int = 0


class AuditRow(BaseModel):
    """Метаданные одного действия без содержимого разговора."""

    id: uuid.UUID
    occurred_at: datetime
    event_type: str
    actor_sub: str
    actor_name: str | None = None
    actor_roles: list[str] = Field(default_factory=list)
    call_id: uuid.UUID | None = None
    request_id: str | None = None
    payload: dict[str, JsonValue] = Field(
        default_factory=dict, json_schema_extra={"additionalProperties": True}
    )


class AuditResponse(Envelope):
    """Страница журнала действий и число подходящих событий."""

    items: list[AuditRow] = Field(default_factory=list)
    total: int = 0


class UserRow(BaseModel):
    """Последняя наблюдавшаяся личность и роли пользователя сервиса."""

    sub: str
    username: str | None = None
    email: str | None = None
    display_name: str | None = None
    roles_last_seen: list[str] = Field(default_factory=list)
    first_seen_at: datetime
    last_seen_at: datetime


class UsersResponse(Envelope):
    """Пользователи, наблюдавшиеся при входе в сервис речи."""

    items: list[UserRow] = Field(default_factory=list)


class ActionResponse(Envelope):
    """Число изменений и пояснение результата административной операции."""

    changed: int = 0
    detail: str | None = None


PROBE_TIMEOUT_SECONDS: Final[float] = 2.0

PAGE_MAX: Final[int] = 500


class ListClassifierQuery(BaseModel):
    """Вход операции list_classifier; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal


class UpdateClassifierNodeQuery(BaseModel):
    """Вход операции update_classifier_node; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    node_id: uuid.UUID
    payload: ClassifierNodePatch
    actor: Principal


class ListProjectsQuery(BaseModel):
    """Вход операции list_projects; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal


class UpdateProjectQuery(BaseModel):
    """Вход операции update_project; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    project_id: uuid.UUID
    payload: ProjectPatch
    actor: Principal


class ReadSettingsQuery(BaseModel):
    """Вход операции read_settings; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal


class WriteSettingsQuery(BaseModel):
    """Вход операции write_settings; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    payload: SettingsPatch
    actor: Principal


class ReadAuditQuery(BaseModel):
    """Вход операции read_audit; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    event_type: str | None = None
    actor_sub: str | None = None
    call_id: uuid.UUID | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = 100
    offset: int = 0


class ListUsersQuery(BaseModel):
    """Вход операции list_users; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal


class PipelineQuery(BaseModel):
    """Вход операции pipeline; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal


class ListJobsQuery(BaseModel):
    """Вход операции list_jobs; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    job_status: JobStatus | None = None
    kind: JobKind | None = None
    lane: JobLane | None = None
    limit: int = 100
    offset: int = 0


class RetryJobQuery(BaseModel):
    """Вход операции retry_job; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    job_id: uuid.UUID
    actor: Principal


class ReprocessCallQuery(BaseModel):
    """Вход операции reprocess_call; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    call_id: uuid.UUID
    actor: Principal
    from_kind: JobKind = JobKind.TRANSCRIBE


class SyncDirectoriesQuery(BaseModel):
    """Вход операции sync_directories; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal


class PauseBackfillQuery(BaseModel):
    """Вход операции pause_backfill; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal


class StartBackfillQuery(BaseModel):
    """Вход операции start_backfill; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
