"""HTTP-маршруты dashboard: валидация параметров и вызов сценариев."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response

from src.application.dto import dashboard as board
from src.application.dto.api import (
    DashboardResponse,
)
from src.application.dto.dashboard_operations import (
    DashboardFileQuery,
    DashboardQuery,
)
from src.presentation.http.auth import PERM_SEARCH, Principal, require
from src.presentation.http.deps import CasesDep
from src.presentation.http.responses import file_response

router = APIRouter(prefix="/api/v1")


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_SEARCH)],
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    period: Annotated[str | None, Query(max_length=8)] = None,
    grain: board.Grain | None = None,
    theme: Annotated[str | None, Query(max_length=200)] = None,
) -> DashboardResponse:
    """Обращения NCC по темам за период: столбцы по времени, негатив, выбранная тема.

    `grain` — корзина столбцов: день, неделя или месяц. Без него сервер
    выбирает сам по длине периода (до 10 дней — дни, до 120 — недели, дальше —
    месяцы); названная корзина, которая дала бы больше ста столбцов, заменяется
    автоматической. Какая применена, сказано в `grain` ответа.

    На графике — шесть самых частых тем периода и «Прочее». `theme` — ключ
    темы из `themes` ответа: по ней считается `selection` — число обращений,
    доля негатива, изменение ко второй половине периода, пиковая неделя и
    подтемы. Без `theme` — по всем темам. Столбцы от выбора не зависят.

    Период — как у поиска: без границ — последние тридцать дней, `period=all`
    — всё время.
    """
    return await cases.dashboard.dashboard(
        DashboardQuery(
            actor=principal,
            date_from=date_from,
            date_to=date_to,
            period=period,
            grain=grain,
            theme=theme,
        )
    )


@router.get("/dashboard.xlsx")
async def dashboard_file(
    cases: CasesDep,
    principal: Annotated[Principal, require(PERM_SEARCH)],
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    period: Annotated[str | None, Query(max_length=8)] = None,
    grain: board.Grain | None = None,
) -> Response:
    """Числа дашборда файлом Excel: темы по времени, темы за период и подтемы.

    Столбцы — те же, что на экране, с той же корзиной. Темы и подтемы — все и по
    отдельности, без «Прочего»: в файле место есть.

    `403` — нет права `export`: дашборд человек видит, а файл наружу не выносит.
    Журнал пишется до сборки файла, как у остальных выгрузок.
    """
    result = await cases.dashboard.dashboard_file(
        DashboardFileQuery(
            actor=principal, date_from=date_from, date_to=date_to, period=period, grain=grain
        )
    )
    return file_response(result)
