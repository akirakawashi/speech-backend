"""Типизированные команды и проекции операций dashboard."""

from __future__ import annotations

from pydantic import ConfigDict

from src.application.authorization import Principal
from src.application.dto import dashboard as board
from src.application.dto.api import DashboardResponse as DashboardResponse
from src.application.dto.period import PeriodQuery


class DashboardQuery(PeriodQuery):
    """Вход операции dashboard; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    grain: board.Grain | None = None
    theme: str | None = None


class DashboardFileQuery(PeriodQuery):
    """Вход операции dashboard_file; транспорт уже разобрал параметры запроса."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    grain: board.Grain | None = None
