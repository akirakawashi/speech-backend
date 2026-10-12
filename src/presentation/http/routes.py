"""HTTP-маршруты calls: валидация параметров и вызов сценариев."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from src.application.dto.api import (
    CallResponse,
    ClassifierResponse,
    MeResponse,
    OptionsResponse,
    SearchResponse,
    StatsResponse,
)
from src.application.dto.calls import (
    CallSource,
    Product,
    Segment,
)
from src.application.dto.calls_operations import (
    CallCardQuery,
    ClassifierQuery,
    MeQuery,
    OperatorsQuery,
    ProjectsQuery,
    SearchQuery,
    StatsQuery,
)
from src.application.dto.search import MAX_PAGE_SIZE, PAGE_SIZE
from src.presentation.http.auth import (
    PERM_SEARCH,
    CurrentPrincipal,
    Principal,
    require,
)
from src.presentation.http.deps import CasesDep

router = APIRouter(prefix="/api/v1")


@router.get("/me", response_model=MeResponse)
async def me(cases: CasesDep, principal: CurrentPrincipal) -> MeResponse:
    """Кто я и что мне позволено.

    Заодно единственное место, где обновляется `speech_users`: администратору
    нужно видеть, кто заходил и с какими правами, не открывая Keycloak.
    """
    return await cases.calls.me(MeQuery(actor=principal))


@router.get("/search", response_model=SearchResponse)
async def search(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_SEARCH)],
    q: Annotated[str, Query(max_length=500)] = "",
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    period: Annotated[str | None, Query(max_length=8)] = None,
    operator_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    segment: Annotated[Segment | None, Query()] = None,
    product: Annotated[Product | None, Query()] = None,
    source: CallSource = CallSource.NCC,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
) -> SearchResponse:
    """Выдача (SRC-01…05). `node_id` — ветка классификатора по классификации ИИ.

    `source` — чьи звонки (S-35), по умолчанию `ncc`; почему так — в описании
    модуля. Доля от периода считается в том же источнике.
    """
    return await cases.calls.search(
        SearchQuery(
            actor=principal,
            q=q,
            date_from=date_from,
            date_to=date_to,
            period=period,
            operator_id=operator_id,
            project_id=project_id,
            node_id=node_id,
            segment=segment,
            product=product,
            source=source,
            page=page,
            size=size,
        )
    )


@router.get("/stats", response_model=StatsResponse)
async def stats(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_SEARCH)],
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    period: Annotated[str | None, Query(max_length=8)] = None,
    source: CallSource = CallSource.NCC,
) -> StatsResponse:
    """KPI периода и десять главных тем (SRC-07, SRC-08).

    «Расшифровано» — звонки, у которых есть расшифровка: с этого момента текст
    находится поиском и открывается в карточке.

    `source` — тот же, что у выдачи (S-35): KPI над списком Телфина, посчитанные
    по NCC, описывали бы не тот список.
    """
    return await cases.calls.stats(
        StatsQuery(
            actor=principal, date_from=date_from, date_to=date_to, period=period, source=source
        )
    )


@router.get("/calls/{call_id}", response_model=CallResponse)
async def call_card(
    cases: CasesDep,
    call_id: uuid.UUID,
    principal: Annotated[Principal, require(PERM_SEARCH)],
    q: Annotated[str, Query(max_length=500)] = "",
) -> CallResponse:
    """Карточка звонка с репликами.

    `q` не фильтрует реплики, а подсвечивает: пользователь пришёл сюда из
    выдачи и должен видеть весь разговор, а не только совпавшие места.

    Параметра `source` здесь нет намеренно (S-35): звонок открывается по своему
    идентификатору, чей бы он ни был, а источник приходит полем ответа.
    """
    return await cases.calls.call_card(CallCardQuery(call_id=call_id, actor=principal, q=q))


@router.get("/operators", response_model=OptionsResponse)
async def operators(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_SEARCH)],
    source: CallSource = CallSource.NCC,
) -> OptionsResponse:
    """Значения фильтра «оператор» — только те, у кого есть звонки.

    Иначе список растёт бывшими сотрудниками, и выбрать из него можно то, что
    ничего не найдёт. По той же причине — только звонки источника `source`
    (S-35): оператор Телфина во вкладке NCC — тоже выбор пустоты.
    """
    return await cases.calls.operators(OperatorsQuery(actor=principal, source=source))


@router.get("/projects", response_model=OptionsResponse)
async def projects(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_SEARCH)],
    source: CallSource = CallSource.NCC,
) -> OptionsResponse:
    """Значения фильтра «очередь» — проекты со звонками источника `source` (S-35).

    Проект Телфина лежит в той же таблице проектов, что и очереди NCC, и без
    условия на источник попадал бы в список вкладки NCC. Во вкладке Телфина
    экран зовёт этот фильтр «проект»: у исходящих звонков очереди нет.
    """
    return await cases.calls.projects(ProjectsQuery(actor=principal, source=source))


@router.get("/classifier", response_model=ClassifierResponse)
async def classifier(
    cases: CasesDep, principal: Annotated[Principal, require(PERM_SEARCH)]
) -> ClassifierResponse:
    """Дерево классификатора для фильтра «Классификация» (SP-310) — плоско.

    С выключенными узлами: у них есть звонки, и ссылка с таким узлом должна
    показать, чем отфильтрована выдача. Предлагать ли их для нового выбора,
    решает экран.
    """
    return await cases.calls.classifier(ClassifierQuery(actor=principal))
