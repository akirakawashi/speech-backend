"""HTTP-маршруты quality_read: валидация параметров и вызов сценариев."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response

from src.application.dto.api import (
    CallQualityResponse,
    QualityCallsResponse,
    QualitySummaryResponse,
)
from src.application.dto.quality_read_operations import (
    CALLS_PAGE_MAX as CALLS_PAGE_MAX,
    CALLS_PAGE_SIZE as CALLS_PAGE_SIZE,
    Bucket as Bucket,
    CallQualityQuery,
    CallSort as CallSort,
    EvaluationStatus as EvaluationStatus,
    QualityCallsQuery,
    QualityReviewFileQuery,
    QualitySummaryQuery,
    StageState as StageState,
)
from src.application.dto.review import REVIEW_MAX, REVIEW_SIZE
from src.presentation.http.auth import PERM_QUALITY, Principal, require
from src.presentation.http.deps import CasesDep
from src.presentation.http.responses import file_response

router = APIRouter(prefix="/api/v1")


@router.get("/calls/{call_id}/quality", response_model=CallQualityResponse)
async def call_quality(
    cases: CasesDep, call_id: uuid.UUID, principal: Annotated[Principal, require(PERM_QUALITY)]
) -> CallQualityResponse:
    """Оценка звонка по текущей редакции скрипта.

    `404 not_evaluated` — оценки нет. `404 not_applicable` — звонок другого
    источника: по скрипту он не оценивается и оценён не будет, поэтому карточка
    не должна обещать оценку.
    """
    return await cases.quality_read.call_quality(CallQualityQuery(call_id=call_id, actor=principal))


@router.get("/quality/summary", response_model=QualitySummaryResponse)
async def quality_summary(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_QUALITY)],
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    period: Annotated[str | None, Query(max_length=8)] = None,
    operator_id: uuid.UUID | None = None,
    unassigned: bool = False,
) -> QualitySummaryResponse:
    """Сводка за период: разряды по баллу, этапы, возражения, итоги, сотрудники, недели.

    Считаются звонки того источника, для которого написан скрипт; остальные в
    сводку не входят ни оценёнными, ни ожидающими.

    Период — как у поиска: без границ — последние тридцать дней, `period=all` —
    всё время. Этапы, возражения и итоги перечислены в порядке скрипта, и в
    списке есть каждый — с нулём, если в периоде его не было: экран не должен
    менять раскладку от того, какие пункты встретились.
    """
    return await cases.quality_read.quality_summary(
        QualitySummaryQuery(
            actor=principal,
            date_from=date_from,
            date_to=date_to,
            period=period,
            operator_id=operator_id,
            unassigned=unassigned,
        )
    )


@router.get("/quality/calls", response_model=QualityCallsResponse)
async def quality_calls(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_QUALITY)],
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    period: Annotated[str | None, Query(max_length=8)] = None,
    operator_id: uuid.UUID | None = None,
    unassigned: bool = False,
    applies: bool | None = None,
    evaluation_status: EvaluationStatus | None = None,
    bucket: Bucket | None = None,
    stage: Annotated[str | None, Query(max_length=64)] = None,
    stage_state: StageState = "failed",
    outcome: Annotated[str | None, Query(max_length=32)] = None,
    violations: bool = False,
    sort: CallSort = "newest",
    limit: Annotated[int, Query(ge=1, le=CALLS_PAGE_MAX)] = CALLS_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QualityCallsResponse:
    """Записи периода из источника скрипта с фильтрами и порядком.

    По умолчанию видны также ошибки оценки, ожидающие её звонки и звонки, у
    которых расшифровки уже не будет (`unavailable`).
    `evaluation_status` отбирает состояние оценки; `applies=false` — звонки
    вне скрипта с причиной в `summary`. Баллы и этапы относятся только к
    актуальным успешным оценкам.

    `bucket` — разряд по баллу, `outcome` — итог звонка, `violations` — не
    выполнено обязательное условие. `stage` — этап, а `stage_state` — каким он
    был в звонке: не выполнен (так по умолчанию — ищут обычно ошибки), выполнен
    или не понадобился. По второму находят звонки, на которые стоит равняться.
    Неизвестный этап или итог — `422`, а не пустой список: опечатка в фильтре
    не должна выглядеть как «таких звонков нет».
    """
    return await cases.quality_read.quality_calls(
        QualityCallsQuery(
            actor=principal,
            date_from=date_from,
            date_to=date_to,
            period=period,
            operator_id=operator_id,
            unassigned=unassigned,
            applies=applies,
            evaluation_status=evaluation_status,
            bucket=bucket,
            stage=stage,
            stage_state=stage_state,
            outcome=outcome,
            violations=violations,
            sort=sort,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/quality/review.xlsx")
async def quality_review_file(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_QUALITY)],
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    period: Annotated[str | None, Query(max_length=8)] = None,
    operator_id: uuid.UUID | None = None,
    unassigned: bool = False,
    limit: Annotated[int, Query(ge=1, le=REVIEW_MAX)] = REVIEW_SIZE,
) -> Response:
    """Файл Excel для сверки оценок контролёром: случайные оценённые звонки охвата.

    Охват — тот же, что у сводки: период и сотрудник. Каждый запрос — новая
    случайная выборка. В файле оценка модели по каждому этапу, её пояснение,
    реплики-основания и полный текст разговоров; что куда
    ложится — в `src.infrastructure.reports.quality`.

    `403` — нет права `export`: оценки человек видит, а файл наружу не выносит.
    `404 nothing_to_review` — в охвате нет ни одного звонка, оценённого по
    скрипту: пустой файл выглядел бы как сбой выгрузки.

    Журнал пишется до сборки файла и в той же транзакции, что и выборка, — как
    у отчёта по звонкам: выгрузка, о которой журнал не знает, хуже записи о
    файле, который не собрался.
    """
    result = await cases.quality_read.quality_review_file(
        QualityReviewFileQuery(
            actor=principal,
            date_from=date_from,
            date_to=date_to,
            period=period,
            operator_id=operator_id,
            unassigned=unassigned,
            limit=limit,
        )
    )
    return file_response(result)
