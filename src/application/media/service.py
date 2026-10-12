"""Прослушивание записи, явное скачивание и экспорт результатов поиска."""

from src.application.authorization import (
    PERM_DOWNLOAD_AUDIO,
    PERM_EXPORT,
    PERM_SEARCH,
    ensure_permission,
)
from src.application.calls.period import is_all_time, resolve_period
from src.application.dto.files import FileResult
from src.application.dto.media_access import (
    EXPORT_LIMIT,
    AudioQuery,
    ExportAudit,
    ExportQuery,
    RecordingStream,
)
from src.application.dto.search import MAX_PAGE_SIZE, SearchFilters, SearchItem, parse_mode
from src.application.ports.media_access import AudioReader, CsvRenderer, ExportRepository


class MediaService:
    """Проверить доступ до чтения данных и последовательно выполнить экспорт."""

    def __init__(self, audio: AudioReader, export: ExportRepository, renderer: CsvRenderer) -> None:
        """Принять заменяемые порты чтения аудио, данных и CSV-кодека."""
        self._audio = audio
        self._export = export
        self._renderer = renderer

    async def audio(self, query: AudioQuery) -> RecordingStream:
        """Проверить права поиска и скачивания, затем открыть запись через порт."""
        ensure_permission(query.actor, PERM_SEARCH)
        if query.download:
            ensure_permission(query.actor, PERM_DOWNLOAD_AUDIO)
        return await self._audio.open(query)

    async def export(self, query: ExportQuery) -> FileResult:
        """Пройти ограниченную поисковую выборку, записать аудит и вернуть CSV."""
        ensure_permission(query.actor, PERM_EXPORT)
        start, end = resolve_period(
            query.date_from, query.date_to, all_time=is_all_time(query.period)
        )
        filters = SearchFilters(
            date_from=start,
            date_to=end,
            operator_id=query.operator_id,
            project_id=query.project_id,
            node_id=query.node_id,
            segment=query.segment.value if query.segment else None,
            product=query.product.value if query.product else None,
            source=query.source.value,
        )
        items: list[SearchItem] = []
        for offset in range(0, EXPORT_LIMIT, MAX_PAGE_SIZE):
            size = min(MAX_PAGE_SIZE, EXPORT_LIMIT - offset)
            page = await self._export.search(query.q, filters, limit=size, offset=offset)
            items.extend(page.items)
            if len(page.items) < size or len(items) >= page.total_calls:
                break
        await self._export.record_export(
            ExportAudit(
                actor=query.actor,
                rows=len(items),
                query_length=len(query.q.strip()),
                mode=parse_mode(query.q),
                source=query.source.value,
            )
        )
        return self._renderer.render(items, query.source)
