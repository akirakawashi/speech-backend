"""SQL и внешние адаптеры операций dashboard."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.api import (
    DashboardBucket,
    DashboardPeak,
    DashboardResponse,
    DashboardSelection,
    DashboardSubtopic,
    DashboardTheme,
)
from src.application.dto.calls import CallSource
from src.application.dto.dashboard_operations import (
    DashboardFileQuery,
    DashboardQuery,
)
from src.application.dto.files import XLSX_MEDIA_TYPE, FileResult
from src.application.errors.requests import ApplicationError, ErrorKind
from src.infrastructure.persistence import dashboard as board
from src.infrastructure.persistence.http_audit import record
from src.infrastructure.reports import dashboard as dashboard_xlsx
from src.setting.config import Settings

SOURCE: Final[str] = CallSource.NCC.value


async def _build(
    session: AsyncSession,
    *,
    timezone: str,
    date_from: datetime | None,
    date_to: datetime | None,
    grain: board.Grain | None,
    theme: str | None,
) -> board.Dashboard:
    """Счёты периода — в дашборд. Период в запросе — мгновения, на экране — сутки.

    Границы приходят так же, как у поиска и оценки: начало суток в поясе
    колл-центра и начало **следующих** суток. Без верхней границы период
    кончается сегодня, без нижней — начинается с первого звонка.
    """
    start, end = date_from, date_to
    zone = ZoneInfo(timezone)
    rows, labels = await board.load_counts(
        session, source=SOURCE, date_from=start, date_to=end, timezone=timezone
    )
    last: date = (
        (end - timedelta(microseconds=1)).astimezone(zone).date()
        if end is not None
        else datetime.now(UTC).astimezone(zone).date()
    )
    if start is not None:
        first: date = start.astimezone(zone).date()
    else:
        first = min((row.day for row in rows), default=last)
    if last < first:
        raise ApplicationError(kind=ErrorKind.INVALID_INPUT, detail="period_is_empty")
    return board.build(
        rows, first=first, last=last, grain=grain, theme=theme or None, known_labels=labels
    )


class PostgresDashboardRepository:
    """Реализовать операции dashboard в сессии и ресурсах вызывающего."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """Получить общую сессию запроса и явную конфигурацию адаптеров."""
        self._session = session
        self._settings = settings

    async def dashboard(self, command: DashboardQuery) -> DashboardResponse:
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
        session = self._session
        settings = self._settings
        date_from = command.date_from
        date_to = command.date_to
        grain = command.grain
        theme = command.theme
        built = await _build(
            session,
            timezone=settings.speech_display_timezone,
            date_from=date_from,
            date_to=date_to,
            grain=grain,
            theme=theme,
        )
        chosen = built.selection
        return DashboardResponse(
            first_day=built.first_day,
            last_day=built.last_day,
            grain=built.grain,
            calls=built.calls,
            themes=[
                DashboardTheme(
                    key=item.key,
                    label=item.label,
                    calls=item.calls,
                    share=item.share,
                    negative_share=item.negative_share,
                )
                for item in built.themes
            ],
            buckets=[
                DashboardBucket(
                    start=item.start,
                    end=item.end,
                    calls=item.calls,
                    by_theme=list(item.by_theme),
                    negative_share=item.negative_share,
                )
                for item in built.buckets
            ],
            selection=DashboardSelection(
                theme=chosen.theme,
                label=chosen.label,
                calls=chosen.calls,
                negative_share=chosen.negative_share,
                change=chosen.change,
                peak=DashboardPeak(
                    start=chosen.peak.start, end=chosen.peak.end, calls=chosen.peak.calls
                )
                if chosen.peak
                else None,
                subtopics=[
                    DashboardSubtopic(
                        key=item.key, label=item.label, calls=item.calls, share=item.share
                    )
                    for item in chosen.subtopics
                ],
            ),
        )

    async def dashboard_file(self, command: DashboardFileQuery) -> FileResult:
        """Числа дашборда файлом Excel: темы по времени, темы за период и подтемы.

        Столбцы — те же, что на экране, с той же корзиной. Темы и подтемы — все и по
        отдельности, без «Прочего»: в файле место есть.

        `403` — нет права `export`: дашборд человек видит, а файл наружу не выносит.
        Журнал пишется до сборки файла, как у остальных выгрузок.
        """
        session = self._session
        settings = self._settings
        principal = command.actor
        date_from = command.date_from
        date_to = command.date_to
        grain = command.grain
        built = await _build(
            session,
            timezone=settings.speech_display_timezone,
            date_from=date_from,
            date_to=date_to,
            grain=grain,
            theme=None,
        )
        await record(
            session,
            principal,
            "report.dashboard",
            payload={
                "from": built.first_day.isoformat(),
                "to": built.last_day.isoformat(),
                "grain": built.grain,
                "calls": built.calls,
            },
        )
        await session.commit()
        content = await asyncio.to_thread(dashboard_xlsx.build_workbook, built)
        filename = f"dashboard-{built.first_day.isoformat()}_{built.last_day.isoformat()}.xlsx"
        return FileResult(content=content, media_type=XLSX_MEDIA_TYPE, filename=filename)
