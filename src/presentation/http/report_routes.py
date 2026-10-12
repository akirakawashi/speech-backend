"""HTTP-маршруты reports: валидация параметров и вызов сценариев."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Response

from src.application.dto.calls import CallSource
from src.application.dto.reports_operations import (
    ClassificationNode as ClassificationNode,
    ClassificationResponse as ClassificationResponse,
    ReportCallsQuery,
    ReportClassificationQuery,
)
from src.presentation.http.auth import PERM_ADMIN, Principal, require
from src.presentation.http.deps import CasesDep
from src.presentation.http.responses import file_response

AdminPrincipal = Annotated[Principal, require(PERM_ADMIN)]

router = APIRouter(prefix="/api/v1/admin")


@router.get("/reports/classification", response_model=ClassificationResponse)
async def report_classification(
    cases: CasesDep,
    _admin: AdminPrincipal,
    day_from: Annotated[date, Query(alias="from")],
    day_to: Annotated[date, Query(alias="to")],
    source: CallSource = CallSource.NCC,
) -> ClassificationResponse:
    """Узлы, которые в периоде встречаются, — варианты фильтров отчёта, в порядке дерева.

    Только встречающиеся: список, в котором половина вариантов даёт пустой
    отчёт, предлагает выбрать пустоту. Счёт — по ветке узла, как и отбор.
    """
    return await cases.reports.report_classification(
        ReportClassificationQuery(actor=_admin, day_from=day_from, day_to=day_to, source=source)
    )


@router.get("/reports/calls.xlsx")
async def report_calls(
    cases: CasesDep,
    admin: AdminPrincipal,
    day_from: Annotated[date, Query(alias="from")],
    day_to: Annotated[date, Query(alias="to")],
    operator_id: uuid.UUID | None = None,
    operator_node_id: uuid.UUID | None = None,
    ai_node_id: uuid.UUID | None = None,
    source: CallSource = CallSource.NCC,
) -> Response:
    """Звонки периода со сводками — файлом Excel.

    `operator_node_id` и `ai_node_id` — ветки классификатора по классификации
    оператора и по классификации ИИ. `source` — чьи звонки (S-35): по умолчанию
    входящие NCC, `telphin` — исходящие ШПД; в один отчёт они не смешиваются.

    Журнал пишется до сборки файла и в той же транзакции, что и выборка: так же,
    как у выгрузки списка. Выгрузка, о которой журнал не знает, хуже записи о
    файле, который не собрался. В журнале — и сколько расшифровок ушло в файл,
    и сколько из них обрезано по пределу ячейки.
    """
    result = await cases.reports.report_calls(
        ReportCallsQuery(
            actor=admin,
            day_from=day_from,
            day_to=day_to,
            operator_id=operator_id,
            operator_node_id=operator_node_id,
            ai_node_id=ai_node_id,
            source=source,
        )
    )
    return file_response(result)
