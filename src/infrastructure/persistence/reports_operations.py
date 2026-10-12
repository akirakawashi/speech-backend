"""SQL и внешние адаптеры операций reports."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.calls import CallSource
from src.application.dto.files import XLSX_MEDIA_TYPE, FileResult
from src.application.dto.reports_operations import (
    ClassificationNode,
    ClassificationResponse,
    ReportCallsQuery,
    ReportClassificationQuery,
)
from src.application.errors.requests import ApplicationError, ErrorKind
from src.infrastructure.persistence.classifier import NodePaths, path_caption
from src.infrastructure.persistence.http_audit import record
from src.infrastructure.reports import calls as report
from src.setting.config import Settings

_OPERATOR_SQL = text("SELECT login FROM speech_operators WHERE id = :id")

_BUILDING = asyncio.Semaphore(1)


def _bounds(day_from: date, day_to: date, timezone: str) -> tuple[datetime, datetime]:
    """Разрешить календарные границы запроса отчёта."""
    if day_to < day_from:
        raise ApplicationError(kind=ErrorKind.INVALID_INPUT, detail="period_is_empty")
    return report.period_bounds(day_from, day_to, timezone)


async def _describe_filters(
    session: AsyncSession, filters: report.ReportFilters
) -> list[tuple[str, str]]:
    """Фильтры словами для листа «Отчёт»: идентификатор человеку ничего не говорит."""
    described: list[tuple[str, str]] = []
    if filters.source == CallSource.TELPHIN:
        described.append(("Источник", "Телфин · исходящие ШПД"))
    if filters.operator_id:
        login = await session.scalar(_OPERATOR_SQL, {"id": filters.operator_id})
        described.append(("Оператор", str(login or filters.operator_id)))
    if filters.operator_node_id or filters.ai_node_id:
        paths = await NodePaths.load(session)
        for title, node_id in (
            ("Классификация оператора (с веткой)", filters.operator_node_id),
            ("Классификация ИИ (с веткой)", filters.ai_node_id),
        ):
            if node_id:
                described.append((title, path_caption(paths.labels(node_id)) or str(node_id)))
    return described


class PostgresReportsRepository:
    """Реализовать операции reports в сессии и ресурсах вызывающего."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """Получить общую сессию запроса и явную конфигурацию адаптеров."""
        self._session = session
        self._settings = settings

    async def report_classification(
        self, command: ReportClassificationQuery
    ) -> ClassificationResponse:
        """Узлы, которые в периоде встречаются, — варианты фильтров отчёта, в порядке дерева.

        Только встречающиеся: список, в котором половина вариантов даёт пустой
        отчёт, предлагает выбрать пустоту. Счёт — по ветке узла, как и отбор.
        """
        session = self._session
        settings = self._settings
        day_from = command.day_from
        day_to = command.day_to
        source = command.source
        start, end = _bounds(day_from, day_to, settings.speech_display_timezone)
        options = await report.list_classification(session, start, end, source.value)
        return ClassificationResponse(
            items=[
                ClassificationNode(
                    id=option.node_id,
                    parent_id=option.parent_id,
                    level=option.level,
                    label=option.label,
                    sort_order=option.sort_order,
                    operator_calls=option.operator_calls,
                    ai_calls=option.ai_calls,
                )
                for option in options
            ]
        )

    async def report_calls(self, command: ReportCallsQuery) -> FileResult:
        """Звонки периода со сводками — файлом Excel.

        `operator_node_id` и `ai_node_id` — ветки классификатора по классификации
        оператора и по классификации ИИ. `source` — чьи звонки (S-35): по умолчанию
        входящие NCC, `telphin` — исходящие ШПД; в один отчёт они не смешиваются.

        Журнал пишется до сборки файла и в той же транзакции, что и выборка: так же,
        как у выгрузки списка. Выгрузка, о которой журнал не знает, хуже записи о
        файле, который не собрался. В журнале — и сколько расшифровок ушло в файл,
        и сколько из них обрезано по пределу ячейки.
        """
        session = self._session
        settings = self._settings
        admin = command.actor
        day_from = command.day_from
        day_to = command.day_to
        operator_id = command.operator_id
        operator_node_id = command.operator_node_id
        ai_node_id = command.ai_node_id
        source = command.source
        start, end = _bounds(day_from, day_to, settings.speech_display_timezone)
        filters = report.ReportFilters(
            date_from=start,
            date_to=end,
            operator_id=operator_id,
            operator_node_id=operator_node_id,
            ai_node_id=ai_node_id,
            source=source.value,
        )
        if _BUILDING.locked():
            raise ApplicationError(kind=ErrorKind.BUSY, detail="report_busy")
        async with _BUILDING:
            try:
                rows = await report.load_report(session, filters, limit=report.REPORT_LIMIT)
            except report.ReportTooLarge as exc:
                raise ApplicationError(
                    kind=ErrorKind.INVALID_INPUT,
                    detail=f"report_too_large: {exc.rows} calls, limit {exc.limit}",
                ) from exc
            described = await _describe_filters(session, filters)
            reasons = await report.load_reasons(session)
            transcripts, truncated = report.transcript_counts(rows)
            await record(
                session,
                admin,
                "report.calls",
                payload={
                    "from": day_from.isoformat(),
                    "to": day_to.isoformat(),
                    "operator_id": str(operator_id) if operator_id else None,
                    "operator_node_id": str(operator_node_id) if operator_node_id else None,
                    "ai_node_id": str(ai_node_id) if ai_node_id else None,
                    "source": source.value,
                    "rows": len(rows),
                    "transcripts": transcripts,
                    "transcripts_truncated": truncated,
                },
            )
            await session.commit()
            content = await asyncio.to_thread(
                report.build_workbook,
                rows,
                period=(day_from, day_to),
                filters=described,
                timezone=settings.speech_display_timezone,
                base_url=settings.speech_public_base_url,
                generated_at=datetime.now(UTC),
                reasons=reasons,
            )
        prefix = "speech-report" if source == CallSource.NCC else f"speech-report-{source.value}"
        filename = f"{prefix}-{day_from.isoformat()}_{day_to.isoformat()}.xlsx"
        return FileResult(content=content, media_type=XLSX_MEDIA_TYPE, filename=filename)
