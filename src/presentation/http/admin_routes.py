"""HTTP-маршруты admin: валидация параметров и вызов сценариев."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from src.application.dto.admin_operations import (
    PAGE_MAX as PAGE_MAX,
    PROBE_TIMEOUT_SECONDS as PROBE_TIMEOUT_SECONDS,
    ActionResponse as ActionResponse,
    AuditResponse as AuditResponse,
    AuditRow as AuditRow,
    ClassifierAdminResponse as ClassifierAdminResponse,
    ClassifierNodePatch as ClassifierNodePatch,
    ClassifierNodeRow as ClassifierNodeRow,
    DependencyRow as DependencyRow,
    JobRow as JobRow,
    JobsResponse as JobsResponse,
    LaneRow as LaneRow,
    ListClassifierQuery,
    ListJobsQuery,
    ListProjectsQuery,
    ListUsersQuery,
    PauseBackfillQuery,
    PipelineQuery,
    PipelineResponse as PipelineResponse,
    ProjectPatch as ProjectPatch,
    ProjectRow as ProjectRow,
    ProjectsAdminResponse as ProjectsAdminResponse,
    ReadAuditQuery,
    ReadSettingsQuery,
    ReprocessCallQuery,
    RetryJobQuery,
    SettingsPatch as SettingsPatch,
    SettingsResponse as SettingsResponse,
    StartBackfillQuery,
    SyncDirectoriesQuery,
    UpdateClassifierNodeQuery,
    UpdateProjectQuery,
    UserRow as UserRow,
    UsersResponse as UsersResponse,
    WriteSettingsQuery,
)
from src.application.dto.calls import (
    JobKind,
    JobLane,
    JobStatus,
)
from src.presentation.http.auth import PERM_ADMIN, Principal, require
from src.presentation.http.deps import CasesDep

AdminPrincipal = Annotated[Principal, require(PERM_ADMIN)]

router = APIRouter(prefix="/api/v1/admin")


@router.get("/classifier", response_model=ClassifierAdminResponse)
async def list_classifier(cases: CasesDep, _admin: AdminPrincipal) -> ClassifierAdminResponse:
    """Дерево классификатора плоским списком: у каждого узла — ссылка на родителя.

    Плоским, а не вложенным: дерево из полутысячи узлов собирает экран, а
    вложенный ответ пришлось бы разворачивать обратно ради счёта и отбора.
    """
    return await cases.admin.list_classifier(ListClassifierQuery(actor=_admin))


@router.patch("/classifier/{node_id}", response_model=ClassifierNodeRow)
async def update_classifier_node(
    cases: CasesDep, node_id: uuid.UUID, payload: ClassifierNodePatch, admin: AdminPrincipal
) -> ClassifierNodeRow:
    """Выключить узел или включить обратно.

    Выключенный узел модели не предлагается — вместе со всей веткой под ним.
    Звонки, которые им уже классифицированы, остаются как есть: история
    разметки не переписывается (ADM-01). Удаления нет по той же причине.
    """
    return await cases.admin.update_classifier_node(
        UpdateClassifierNodeQuery(node_id=node_id, payload=payload, actor=admin)
    )


@router.get("/projects", response_model=ProjectsAdminResponse)
async def list_projects(cases: CasesDep, _admin: AdminPrincipal) -> ProjectsAdminResponse:
    """Принять параметры list_projects и вернуть ответ сценария."""
    return await cases.admin.list_projects(ListProjectsQuery(actor=_admin))


@router.patch("/projects/{project_id}", response_model=ProjectRow)
async def update_project(
    cases: CasesDep, project_id: uuid.UUID, payload: ProjectPatch, admin: AdminPrincipal
) -> ProjectRow:
    """Рубильник сбора и сопоставление очереди с сегментом и продуктом (S-21).

    Сопоставление здесь — **факт**, а не догадка: у сопоставленной очереди
    значение сегмента берётся из неё, и модель об этом даже не спрашивают. Из-за
    этого правка одной строки меняет то, как будут размечены все следующие
    звонки очереди, — и оттого пишется в журнал отдельным типом события.

    Уже размеченные звонки не переписываются: отчёт, построенный вчера,
    сегодня не должен показывать другие числа. Для перестановки задним числом
    есть переобработка.
    """
    return await cases.admin.update_project(
        UpdateProjectQuery(project_id=project_id, payload=payload, actor=admin)
    )


@router.get("/settings", response_model=SettingsResponse)
async def read_settings(cases: CasesDep, _admin: AdminPrincipal) -> SettingsResponse:
    """Принять параметры read_settings и вернуть ответ сценария."""
    return await cases.admin.read_settings(ReadSettingsQuery(actor=_admin))


@router.put("/settings", response_model=SettingsResponse)
async def write_settings(
    cases: CasesDep, payload: SettingsPatch, admin: AdminPrincipal
) -> SettingsResponse:
    """Записать настройки.

    Ключ вне объявленного набора отклоняется целиком, а не пропускается: приняв
    его, интерфейс показывал бы сохранённое значение, которого никто не читает.

    Тип сверяется с типом умолчания. `retention_days` строкой — это срок
    хранения, который не применится, и узнать об этом можно было бы только по
    тому, что записи не удаляются.
    """
    return await cases.admin.write_settings(WriteSettingsQuery(payload=payload, actor=admin))


@router.get("/audit", response_model=AuditResponse)
async def read_audit(
    cases: CasesDep,
    _admin: AdminPrincipal,
    event_type: Annotated[str | None, Query(max_length=64)] = None,
    actor_sub: Annotated[str | None, Query(max_length=255)] = None,
    call_id: uuid.UUID | None = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditResponse:
    """Кто что смотрел и кто что менял.

    Фильтр по типу события — главный: закрытый набор типов заведён ровно ради
    того, чтобы этот фильтр работал.
    """
    return await cases.admin.read_audit(
        ReadAuditQuery(
            actor=_admin,
            event_type=event_type,
            actor_sub=actor_sub,
            call_id=call_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/users", response_model=UsersResponse)
async def list_users(cases: CasesDep, _admin: AdminPrincipal) -> UsersResponse:
    """Кто заходил и с какими правами.

    Источник правды по ролям — Keycloak; здесь последнее увиденное значение,
    чтобы администратор мог понять, с чем человек заходил, не открывая Keycloak.
    """
    return await cases.admin.list_users(ListUsersQuery(actor=_admin))


@router.get("/pipeline", response_model=PipelineResponse)
async def pipeline(cases: CasesDep, _admin: AdminPrincipal) -> PipelineResponse:
    """Снимок очереди и состояние зависимостей.

    Пробы идут **параллельно** и каждая со своим потолком: последовательные, с
    таймаутом по умолчанию, они превращают открытие панели в двухминутное
    ожидание ровно тогда, когда что-то сломалось.
    """
    return await cases.admin.pipeline(PipelineQuery(actor=_admin))


@router.get("/jobs", response_model=JobsResponse)
async def list_jobs(
    cases: CasesDep,
    _admin: AdminPrincipal,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    kind: JobKind | None = None,
    lane: JobLane | None = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobsResponse:
    """Принять параметры list_jobs и вернуть ответ сценария."""
    return await cases.admin.list_jobs(
        ListJobsQuery(
            actor=_admin, job_status=job_status, kind=kind, lane=lane, limit=limit, offset=offset
        )
    )


@router.post("/jobs/{job_id}/retry", response_model=ActionResponse)
async def retry_job(cases: CasesDep, job_id: uuid.UUID, admin: AdminPrincipal) -> ActionResponse:
    """Вернуть в очередь задание, которое само не повторится.

    Повторяются только `failed` и `dead`. Взять `running` значило бы завести
    второго исполнителя того же задания рядом с живым первым; `queued` уже
    ждёт, а `done` переигрывать нечего — для этого есть переобработка.

    Счётчик попыток сбрасывается: администратор, нажавший «повторить», принял
    решение, что причина устранена, и оставленный счётчик уронил бы задание
    обратно в `dead` с первой же попытки.
    """
    return await cases.admin.retry_job(RetryJobQuery(job_id=job_id, actor=admin))


@router.post("/calls/{call_id}/reprocess", response_model=ActionResponse)
async def reprocess_call(
    cases: CasesDep,
    call_id: uuid.UUID,
    admin: AdminPrincipal,
    from_kind: Annotated[JobKind, Query(alias="from")] = JobKind.TRANSCRIBE,
) -> ActionResponse:
    """Прогнать звонок заново, начиная с указанного этапа.

    Заводится **одно** задание — того этапа, с которого просили. Следующие
    поставит воркер, закончив это: цепочка уже описана в `SPEECH_NEXT_JOB_KIND`,
    и вторая её копия здесь рано или поздно разойдётся с первой.

    Завершённое задание сбрасывается на месте: уникальность `(call_id, kind)`
    действует на все статусы. Любой активный этап запрещает переобработку.
    """
    return await cases.admin.reprocess_call(
        ReprocessCallQuery(call_id=call_id, actor=admin, from_kind=from_kind)
    )


@router.post("/projects/sync", response_model=ActionResponse)
async def sync_directories(cases: CasesDep, admin: AdminPrincipal) -> ActionResponse:
    """Подтянуть справочники из NCC: сотрудников и известные проекты.

    Одна ручка на оба, а не две. Администратор нажимает её, чтобы получить
    свежие данные из NCC, а не чтобы выбрать между справочниками; разделение
    потребовало бы от него знать, какой из них откуда берётся.

    **Новые проекты здесь не появляются.** Списочного запроса для проектов в
    REST API NCC нет: обновляются те, что мы уже знаем из принятых звонков.
    Это ограничение источника, и ответ говорит о нём словами — «обновлено 0»
    на пустом справочнике иначе читается как поломка.

    Отказ настройки отделён от отказа NCC. Незаданный адрес или токен — это
    `503`: чинить надо развёртывание, и повторным нажатием кнопки этого не
    сделать.
    """
    return await cases.admin.sync_directories(SyncDirectoriesQuery(actor=admin))


@router.post("/backfill/pause", response_model=ActionResponse)
async def pause_backfill(cases: CasesDep, admin: AdminPrincipal) -> ActionResponse:
    """Остановить разбор истории: и перечисление дат, и уже поставленные задания.

    Обе стороны сразу, потому что администратор имеет в виду именно это.
    Останавливая только задания, мы получили бы видимость остановки: курсор
    продолжал бы идти по датам, ставя новые задания, тут же откладываемые, —
    источник опрашивают, очередь растёт, а на экране «приостановлено».

    Задания не удаляются и не помечаются отказом: отложенные далеко в будущее,
    они остаются в очереди и вернутся одним `resume`. Удаление означало бы, что
    возобновление — это заново собранная история, а не продолжение.
    """
    return await cases.admin.pause_backfill(PauseBackfillQuery(actor=admin))


@router.post("/backfill/start", response_model=ActionResponse)
async def start_backfill(cases: CasesDep, admin: AdminPrincipal) -> ActionResponse:
    """Вернуть разбор истории в работу: и задания, и перечисление дат.

    Окно здесь не открывается. Открыть его — решение о том, на сколько месяцев
    назад идти, и принимается оно один раз, в командной строке
    (`python -m src.presentation.cli.backfill start --days N`). Кнопка, молча открывающая
    окно по умолчанию, однажды запустила бы разбор шестидесяти суток от одного
    нажатия не тем человеком.
    """
    return await cases.admin.start_backfill(StartBackfillQuery(actor=admin))
