"""SQL и внешние адаптеры операций admin."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.admin_operations import (
    ActionResponse,
    AuditResponse,
    AuditRow,
    ClassifierAdminResponse,
    ClassifierNodeRow,
    DependencyRow,
    JobRow,
    JobsResponse,
    LaneRow,
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
    UserRow,
    UsersResponse,
    WriteSettingsQuery,
)
from src.application.dto.backfill import BackfillNotStarted
from src.application.dto.calls import SPEECH_DEFAULT_SETTINGS, JobStatus
from src.application.errors.directories import DirectorySourceError
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.queue import (
    ReprocessingCallNotFound,
    ReprocessingConflict,
)
from src.infrastructure.persistence.backfill import set_paused_in
from src.infrastructure.persistence.http_audit import record
from src.infrastructure.persistence.reprocessing import reprocess, retry
from src.infrastructure.queue.sql import WAITING_SINCE_SQL
from src.infrastructure.sources.ncc.rest import NccNotConfigured, build_ncc_rest_client
from src.infrastructure.sources.ncc.sync import sync_employees_in, sync_projects_in
from src.setting.config import Settings

PROBE_TIMEOUT_SECONDS: Final[float] = 2.0

PAGE_MAX: Final[int] = 500

_CLASSIFIER_SQL = text(
    """
    SELECT n.id, n.parent_id, n.level, n.label, n.source, n.sort_order, n.is_active,
           n.first_seen_at, COALESCE(c.calls, 0) AS operator_calls
      FROM speech_classifier_nodes AS n
      LEFT JOIN (
            SELECT operator_node_id, count(*) AS calls
              FROM speech_calls
             WHERE operator_node_id IS NOT NULL
             GROUP BY operator_node_id
      ) AS c ON c.operator_node_id = n.id
     ORDER BY n.level, n.sort_order, n.label
    """
)

_CLASSIFIER_NODE_SQL = text(
    """
    SELECT n.id, n.parent_id, n.level, n.label, n.source, n.sort_order, n.is_active,
           n.first_seen_at,
           (SELECT count(*) FROM speech_calls AS c WHERE c.operator_node_id = n.id)
               AS operator_calls
      FROM speech_classifier_nodes AS n
     WHERE n.id = :id
    """
)

_PROJECTS_SQL = text(
    """
    SELECT p.id, p.ncc_uuid, p.source_key, p.title, p.data_channel, p.direction,
           p.segment, p.product, p.ingest_enabled, p.last_call_at, p.synced_at,
           (SELECT count(*) FROM speech_calls c WHERE c.project_id = p.id) AS calls
      FROM speech_ncc_projects p
     ORDER BY p.title
    """
)


def _json(value: Any) -> str:
    """Закодировать значение для передачи параметром JSON в SQL."""
    import json

    return json.dumps(value, ensure_ascii=False)


_LANES_SQL = text(
    f"""
    SELECT lane, status, count(*) AS jobs,
           extract(epoch FROM now() - min({WAITING_SINCE_SQL})
                   FILTER (WHERE {WAITING_SINCE_SQL} <= now())) AS oldest_seconds
      FROM speech_jobs
     GROUP BY lane, status
     ORDER BY lane, status
    """
)

_ERRORS_SQL = text(
    """
-- То же множество, что у счётчика «сбоев за сутки» ниже, и это обязательно.
--
-- Разбивка и итог на одном экране обязаны описывать одно и то же: иначе сумма
-- по классам не сходится с числом над ней, и разбираться в этом приходится
-- ровно тогда, когда некогда.
--
-- Почему только нерешённое и почему `<> 'done'` — см. `_ERRORS_SQL` в очереди.
    SELECT coalesce(last_error_class, 'unknown') AS error_class, count(*) AS jobs
      FROM speech_jobs
     WHERE last_error_at > now() - interval '24 hours'
       AND status <> 'done'
     GROUP BY 1
    """
)


async def _probe(
    name: str, configured: bool, probe: Callable[[], Awaitable[None]]
) -> DependencyRow:
    """Одна проба с потолком по времени.

    Недоступность — это ответ, а не ошибка панели: администратор открывает её
    именно тогда, когда что-то не отвечает, и падение всей страницы из-за одной
    зависимости лишает его единственного места, где видно, какой именно.

    Принимается **фабрика**, а не готовая корутина: корутина создаётся в месте
    вызова вне зависимости от `configured`, и ранний выход оставлял бы её
    недожиданной — что Python сообщает предупреждением в сторону, а не ошибкой.
    """
    if not configured:
        return DependencyRow(name=name, configured=False, detail="не настроено")
    try:
        await asyncio.wait_for(probe(), timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return DependencyRow(
            name=name,
            configured=True,
            reachable=False,
            detail=f"нет ответа за {PROBE_TIMEOUT_SECONDS:g} с",
        )
    except Exception as exc:
        return DependencyRow(name=name, configured=True, reachable=False, detail=type(exc).__name__)
    return DependencyRow(name=name, configured=True, reachable=True)


async def _storage_probe(settings: Settings) -> None:
    """Проверить доступность настроенного хранилища записей."""
    from src.infrastructure.storage.recordings import build_storage

    storage = build_storage(settings)
    await storage.exists("__pipeline-probe__/does-not-exist")


async def _http_probe(base_url: str, path: str) -> None:
    """Проверить доступность HTTP-зависимости по адресу и пути."""
    import httpx

    timeout = httpx.Timeout(PROBE_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(base_url.rstrip("/") + path)
    if response.status_code >= 500:
        raise RuntimeError(f"HTTP {response.status_code}")


async def _pause_enumeration(session: AsyncSession, *, paused: bool) -> str:
    """Переключить перечисление дат и сказать словами, что вышло.

    В сеансе запроса, а не в своём: второй движок внутри ручки привязался бы к
    другому циклу событий — он кэширован по URL и закрепляется за первым же
    циклом, в котором создан. Заодно пауза и событие журнала о ней попадают в
    одну транзакцию.

    Окно может быть не открыто — и это не ошибка кнопки: задания в полосе
    `backfill` есть и без него, например после ручного перезапуска. Отвечаем
    честно, вместо того чтобы ронять действие, которое своё дело сделало.
    """
    try:
        state = await set_paused_in(session, paused=paused)
    except BackfillNotStarted:
        return (
            "окно разбора истории не открыто — откройте его командой `python "
            "-m src.presentation.cli.backfill start`"
        )
    return state.describe()


class PostgresAdminRepository:
    """Реализовать операции admin в сессии и ресурсах вызывающего."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """Получить общую сессию запроса и явную конфигурацию адаптеров."""
        self._session = session
        self._settings = settings

    async def list_classifier(self, command: ListClassifierQuery) -> ClassifierAdminResponse:
        """Дерево классификатора плоским списком: у каждого узла — ссылка на родителя.

        Плоским, а не вложенным: дерево из полутысячи узлов собирает экран, а
        вложенный ответ пришлось бы разворачивать обратно ради счёта и отбора.
        """
        session = self._session
        rows = (await session.execute(_CLASSIFIER_SQL)).mappings().all()
        return ClassifierAdminResponse(items=[ClassifierNodeRow(**dict(row)) for row in rows])

    async def update_classifier_node(self, command: UpdateClassifierNodeQuery) -> ClassifierNodeRow:
        """Выключить узел или включить обратно.

        Выключенный узел модели не предлагается — вместе со всей веткой под ним.
        Звонки, которые им уже классифицированы, остаются как есть: история
        разметки не переписывается (ADM-01). Удаления нет по той же причине.
        """
        session = self._session
        node_id = command.node_id
        payload = command.payload
        admin = command.actor
        current = (await session.execute(_CLASSIFIER_NODE_SQL, {"id": node_id})).mappings().first()
        if current is None:
            raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail="not_found")
        if current["is_active"] == payload.is_active:
            raise ApplicationError(kind=ErrorKind.INVALID_INPUT, detail="nothing_to_change")
        await session.execute(
            text(
                "UPDATE speech_classifier_nodes SET is_active = :is_active, "
                "updated_at = now() WHERE id = :id"
            ),
            {"id": node_id, "is_active": payload.is_active},
        )
        await record(
            session,
            admin,
            "classifier.toggle",
            payload={
                "node_id": str(node_id),
                "level": current["level"],
                "label": current["label"],
                "is_active": payload.is_active,
            },
        )
        await session.commit()
        return ClassifierNodeRow(**{**dict(current), "is_active": payload.is_active})

    async def list_projects(self, command: ListProjectsQuery) -> ProjectsAdminResponse:
        """Прочитать проекты и разрешение их приёма в DTO ответа."""
        session = self._session
        rows = (await session.execute(_PROJECTS_SQL)).mappings().all()
        return ProjectsAdminResponse(items=[ProjectRow(**dict(row)) for row in rows])

    async def update_project(self, command: UpdateProjectQuery) -> ProjectRow:
        """Рубильник сбора и сопоставление очереди с сегментом и продуктом (S-21).

        Сопоставление здесь — **факт**, а не догадка: у сопоставленной очереди
        значение сегмента берётся из неё, и модель об этом даже не спрашивают. Из-за
        этого правка одной строки меняет то, как будут размечены все следующие
        звонки очереди, — и оттого пишется в журнал отдельным типом события.

        Уже размеченные звонки не переписываются: отчёт, построенный вчера,
        сегодня не должен показывать другие числа. Для перестановки задним числом
        есть переобработка.
        """
        session = self._session
        project_id = command.project_id
        payload = command.payload
        admin = command.actor
        fields: dict[str, Any] = {}
        if payload.ingest_enabled is not None:
            fields["ingest_enabled"] = payload.ingest_enabled
        if payload.clear_segment:
            fields["segment"] = None
        elif payload.segment is not None:
            fields["segment"] = payload.segment.value
        if payload.clear_product:
            fields["product"] = None
        elif payload.product is not None:
            fields["product"] = payload.product.value
        if not fields:
            raise ApplicationError(kind=ErrorKind.INVALID_INPUT, detail="nothing_to_change")
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        row = (
            (
                await session.execute(
                    text(
                        (
                            "UPDATE speech_ncc_projects SET "
                            f"{assignments}"
                            ", updated_at = now() WHERE id = :id RETURNING id, ncc_uuid, "
                            "source_key, title, data_channel, direction, segment, product, "
                            "ingest_enabled, last_call_at, synced_at"
                        )
                    ),
                    {**fields, "id": project_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail="not_found")
        if "ingest_enabled" in fields:
            await record(
                session,
                admin,
                "project.ingest_toggle",
                payload={"title": row["title"], "enabled": fields["ingest_enabled"]},
            )
        if "segment" in fields or "product" in fields:
            await record(
                session,
                admin,
                "project.dimensions",
                payload={
                    "title": row["title"],
                    "segment": fields.get("segment"),
                    "product": fields.get("product"),
                },
            )
        await session.commit()
        return ProjectRow(**dict(row), calls=0)

    async def read_settings(self, command: ReadSettingsQuery) -> SettingsResponse:
        """Прочитать значения и переопределённые ключи администраторских настроек."""
        session = self._session
        rows = (
            (await session.execute(text("SELECT key, value FROM speech_settings"))).mappings().all()
        )
        stored = {row["key"]: row["value"] for row in rows}
        values = {key: stored.get(key, default) for key, default in SPEECH_DEFAULT_SETTINGS.items()}
        return SettingsResponse(
            values=values,
            overridden=sorted(key for key in stored if key in SPEECH_DEFAULT_SETTINGS),
        )

    async def write_settings(self, command: WriteSettingsQuery) -> SettingsResponse:
        """Записать настройки.

        Ключ вне объявленного набора отклоняется целиком, а не пропускается: приняв
        его, интерфейс показывал бы сохранённое значение, которого никто не читает.

        Тип сверяется с типом умолчания. `retention_days` строкой — это срок
        хранения, который не применится, и узнать об этом можно было бы только по
        тому, что записи не удаляются.
        """
        session = self._session
        payload = command.payload
        admin = command.actor
        checked = payload.values
        for key, value in checked.items():
            await session.execute(
                text(
                    "INSERT INTO speech_settings (key, value, updated_by, updated_at) "
                    "VALUES (:key, CAST(:value AS jsonb), :who, now()) ON CONFLICT "
                    "(key) DO UPDATE SET value = EXCLUDED.value, updated_by = "
                    "EXCLUDED.updated_by, updated_at = now()"
                ),
                {"key": key, "value": _json(value), "who": admin.sub},
            )
        await record(session, admin, "settings.update", payload={"keys": sorted(payload.values)})
        await session.commit()
        return await self.read_settings(ReadSettingsQuery(actor=admin))

    async def read_audit(self, command: ReadAuditQuery) -> AuditResponse:
        """Кто что смотрел и кто что менял.

        Фильтр по типу события — главный: закрытый набор типов заведён ровно ради
        того, чтобы этот фильтр работал.
        """
        session = self._session
        event_type = command.event_type
        actor_sub = command.actor_sub
        call_id = command.call_id
        date_from = command.date_from
        date_to = command.date_to
        limit = command.limit
        offset = command.offset
        where = ["1 = 1"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if event_type:
            where.append("event_type = :event_type")
            params["event_type"] = event_type
        if actor_sub:
            where.append("actor_sub = :actor_sub")
            params["actor_sub"] = actor_sub
        if call_id:
            where.append("call_id = :call_id")
            params["call_id"] = call_id
        if date_from:
            where.append("occurred_at >= :date_from")
            params["date_from"] = date_from
        if date_to:
            where.append("occurred_at <= :date_to")
            params["date_to"] = date_to
        clause = " AND ".join(where)
        total = await session.scalar(
            text(f"SELECT count(*) FROM speech_audit_events WHERE {clause}"), params
        )
        rows = (
            (
                await session.execute(
                    text(
                        (
                            "SELECT id, occurred_at, event_type, actor_sub, actor_name, "
                            "actor_roles, call_id, request_id, payload FROM "
                            "speech_audit_events WHERE "
                            f"{clause}"
                            " ORDER BY occurred_at DESC LIMIT :limit OFFSET :offset"
                        )
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        return AuditResponse(
            items=[
                AuditRow(
                    **{
                        **dict(row),
                        "actor_roles": list(row["actor_roles"] or []),
                        "payload": dict(row["payload"] or {}),
                    }
                )
                for row in rows
            ],
            total=int(total or 0),
        )

    async def list_users(self, command: ListUsersQuery) -> UsersResponse:
        """Кто заходил и с какими правами.

        Источник правды по ролям — Keycloak; здесь последнее увиденное значение,
        чтобы администратор мог понять, с чем человек заходил, не открывая Keycloak.
        """
        session = self._session
        rows = (
            (
                await session.execute(
                    text(
                        (
                            "SELECT sub, username, email, display_name, roles_last_seen, "
                            "first_seen_at, last_seen_at FROM speech_users ORDER BY "
                            "last_seen_at DESC"
                        )
                    )
                )
            )
            .mappings()
            .all()
        )
        return UsersResponse(
            items=[
                UserRow(**{**dict(row), "roles_last_seen": list(row["roles_last_seen"] or [])})
                for row in rows
            ]
        )

    async def pipeline(self, command: PipelineQuery) -> PipelineResponse:
        """Снимок очереди и состояние зависимостей.

        Пробы идут **параллельно** и каждая со своим потолком: последовательные, с
        таймаутом по умолчанию, они превращают открытие панели в двухминутное
        ожидание ровно тогда, когда что-то сломалось.
        """
        session = self._session
        settings = self._settings
        lanes = [
            LaneRow(
                lane=str(row["lane"]),
                status=str(row["status"]),
                count=int(row["jobs"]),
                oldest_seconds=float(row["oldest_seconds"])
                if row["status"] in (JobStatus.QUEUED.value, JobStatus.RUNNING.value)
                and row["oldest_seconds"] is not None
                else None,
            )
            for row in (await session.execute(_LANES_SQL)).mappings()
        ]
        errors = {
            str(row["error_class"]): int(row["jobs"])
            for row in (await session.execute(_ERRORS_SQL)).mappings()
        }
        dead = sum(row.count for row in lanes if row.status == JobStatus.DEAD.value)
        failed = await session.scalar(
            text(
                "SELECT count(*) FROM speech_jobs WHERE status <> 'done' AND "
                "last_error_at > now() - interval '24 hours'"
            )
        )
        dependencies = await asyncio.gather(
            _probe(
                "storage", bool(settings.speech_storage_base_url), lambda: _storage_probe(settings)
            ),
            _probe(
                "whisper",
                bool(settings.speech_whisper_base_url),
                lambda: _http_probe(str(settings.speech_whisper_base_url), "/models"),
            ),
            _probe(
                "dify",
                bool(settings.speech_dify_base_url),
                lambda: _http_probe(str(settings.speech_dify_base_url), ""),
            ),
        )
        return PipelineResponse(
            lanes=lanes,
            dead_total=dead,
            failed_last_24h=int(failed or 0),
            errors_by_class=errors,
            dependencies=list(dependencies),
        )

    async def list_jobs(self, command: ListJobsQuery) -> JobsResponse:
        """Прочитать страницу заданий по DTO отбора и вернуть типизированный результат."""
        session = self._session
        job_status = command.job_status
        kind = command.kind
        lane = command.lane
        limit = command.limit
        offset = command.offset
        where = ["1 = 1"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if job_status:
            where.append("j.status = :status")
            params["status"] = job_status.value
        if kind:
            where.append("j.kind = :kind")
            params["kind"] = kind.value
        if lane:
            where.append("j.lane = :lane")
            params["lane"] = lane.value
        clause = " AND ".join(where)
        total = await session.scalar(
            text(f"SELECT count(*) FROM speech_jobs j WHERE {clause}"), params
        )
        rows = (
            (
                await session.execute(
                    text(
                        (
                            "SELECT j.id, j.call_id, c.session_id, j.kind, j.lane, j.status, "
                            "j.attempts, j.max_attempts, j.last_error_class, j.last_error_at, "
                            "j.created_at, j.finished_at FROM speech_jobs j JOIN speech_calls "
                            "c ON c.id = j.call_id WHERE "
                            f"{clause}"
                            " ORDER BY j.created_at DESC LIMIT :limit OFFSET :offset"
                        )
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        return JobsResponse(items=[JobRow(**dict(row)) for row in rows], total=int(total or 0))

    async def retry_job(self, command: RetryJobQuery) -> ActionResponse:
        """Вернуть в очередь задание, которое само не повторится.

        Повторяются только `failed` и `dead`. Взять `running` значило бы завести
        второго исполнителя того же задания рядом с живым первым; `queued` уже
        ждёт, а `done` переигрывать нечего — для этого есть переобработка.

        Счётчик попыток сбрасывается: администратор, нажавший «повторить», принял
        решение, что причина устранена, и оставленный счётчик уронил бы задание
        обратно в `dead` с первой же попытки.
        """
        session = self._session
        job_id = command.job_id
        admin = command.actor
        try:
            call_id, kind = await retry(session, job_id)
            await record(session, admin, "job.retry", call_id=call_id, payload={"kind": kind.value})
            await session.commit()
        except (ReprocessingConflict, ReprocessingCallNotFound) as exc:
            await session.rollback()
            raise ApplicationError(kind=ErrorKind.CONFLICT, detail=str(exc)) from exc
        except Exception:
            await session.rollback()
            raise
        return ActionResponse(changed=1)

    async def reprocess_call(self, command: ReprocessCallQuery) -> ActionResponse:
        """Прогнать звонок заново, начиная с указанного этапа.

        Заводится **одно** задание — того этапа, с которого просили. Следующие
        поставит воркер, закончив это: цепочка уже описана в `SPEECH_NEXT_JOB_KIND`,
        и вторая её копия здесь рано или поздно разойдётся с первой.

        Завершённое задание сбрасывается на месте: уникальность `(call_id, kind)`
        действует на все статусы. Любой активный этап запрещает переобработку.
        """
        session = self._session
        call_id = command.call_id
        admin = command.actor
        from_kind = command.from_kind
        try:
            await reprocess(session, call_id, from_kind)
            await record(
                session, admin, "call.reprocess", call_id=call_id, payload={"from": from_kind.value}
            )
            await session.commit()
        except ReprocessingCallNotFound as exc:
            await session.rollback()
            raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail="not_found") from exc
        except ReprocessingConflict as exc:
            await session.rollback()
            raise ApplicationError(kind=ErrorKind.CONFLICT, detail=str(exc)) from exc
        except Exception:
            await session.rollback()
            raise
        return ActionResponse(changed=1, detail=from_kind.value)

    async def sync_directories(self, command: SyncDirectoriesQuery) -> ActionResponse:
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
        session = self._session
        settings = self._settings
        admin = command.actor
        try:
            client = build_ncc_rest_client(settings)
        except NccNotConfigured as exc:
            raise ApplicationError(kind=ErrorKind.UNAVAILABLE, detail="ncc_not_configured") from exc
        try:
            employees = await sync_employees_in(client, session)
            projects = await sync_projects_in(client, session)
        except DirectorySourceError as exc:
            raise ApplicationError(kind=ErrorKind.DEPENDENCY, detail=f"ncc_{exc.reason}") from exc
        changed = employees.changed + projects.changed
        await record(
            session,
            admin,
            "project.sync",
            payload={
                "employees": {
                    "seen": employees.seen,
                    "created": employees.created,
                    "updated": employees.updated,
                },
                "projects": {
                    "seen": projects.seen,
                    "updated": projects.updated,
                    "failed": projects.failed,
                },
            },
        )
        await session.commit()
        return ActionResponse(
            changed=changed, detail="; ".join(report.describe() for report in (employees, projects))
        )

    async def pause_backfill(self, command: PauseBackfillQuery) -> ActionResponse:
        """Остановить разбор истории: и перечисление дат, и уже поставленные задания.

        Обе стороны сразу, потому что администратор имеет в виду именно это.
        Останавливая только задания, мы получили бы видимость остановки: курсор
        продолжал бы идти по датам, ставя новые задания, тут же откладываемые, —
        источник опрашивают, очередь растёт, а на экране «приостановлено».

        Задания не удаляются и не помечаются отказом: отложенные далеко в будущее,
        они остаются в очереди и вернутся одним `resume`. Удаление означало бы, что
        возобновление — это заново собранная история, а не продолжение.
        """
        session = self._session
        admin = command.actor
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                text(
                    "UPDATE speech_jobs SET run_after = now() + interval '100 years' "
                    "WHERE lane = 'backfill' AND status = 'queued' AND run_after < "
                    "now() + interval '50 years'"
                )
            ),
        )
        changed = result.rowcount
        enumeration = await _pause_enumeration(session, paused=True)
        await record(
            session,
            admin,
            "backfill.pause",
            payload={"jobs": int(changed or 0), "enumeration": enumeration},
        )
        await session.commit()
        return ActionResponse(changed=int(changed or 0), detail=enumeration)

    async def start_backfill(self, command: StartBackfillQuery) -> ActionResponse:
        """Вернуть разбор истории в работу: и задания, и перечисление дат.

        Окно здесь не открывается. Открыть его — решение о том, на сколько месяцев
        назад идти, и принимается оно один раз, в командной строке
        (`python -m src.presentation.cli.backfill start --days N`). Кнопка, молча открывающая
        окно по умолчанию, однажды запустила бы разбор шестидесяти суток от одного
        нажатия не тем человеком.
        """
        session = self._session
        admin = command.actor
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                text(
                    "UPDATE speech_jobs SET run_after = now() WHERE lane = 'backfill' "
                    "AND status = 'queued' AND run_after > now() + interval '50 years'"
                )
            ),
        )
        changed = result.rowcount
        enumeration = await _pause_enumeration(session, paused=False)
        await record(
            session,
            admin,
            "backfill.start",
            payload={"jobs": int(changed or 0), "enumeration": enumeration},
        )
        await session.commit()
        return ActionResponse(changed=int(changed or 0), detail=enumeration)
