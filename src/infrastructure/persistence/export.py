"""Реализация данных экспорта с той же SQL-выборкой, что у поиска."""

from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.media_access import ExportAudit
from src.application.dto.search import SearchFilters, SearchPage
from src.infrastructure.persistence.http_audit import record
from src.infrastructure.persistence.search import search_calls


class PostgresExportRepository:
    """Читать страницы и журналировать экспорт в сессии одного запроса."""

    def __init__(self, session: AsyncSession) -> None:
        """Принять общую сессию; не открывать отдельное соединение для аудита."""
        self._session = session

    async def search(
        self, query: str, filters: SearchFilters, *, limit: int, offset: int
    ) -> SearchPage:
        """Передать отбор существующему поисковому SQL и вернуть DTO страницы."""
        return await search_calls(
            self._session, query=query, filters=filters, limit=limit, offset=offset
        )

    async def record_export(self, event: ExportAudit) -> None:
        """Записать число строк и безопасные параметры, затем завершить транзакцию."""
        await record(
            self._session,
            event.actor,
            "export.list",
            payload={
                "rows": [None] * event.rows,
                "query_len": event.query_length,
                "mode": event.mode,
                "source": event.source,
            },
        )
        await self._session.commit()
