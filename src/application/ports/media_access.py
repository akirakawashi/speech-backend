"""Порты потока записи, поиска для экспорта и формирования CSV."""

from collections.abc import Sequence
from typing import Protocol

from src.application.dto.calls import CallSource
from src.application.dto.files import FileResult
from src.application.dto.media_access import AudioQuery, ExportAudit, RecordingStream
from src.application.dto.search import SearchFilters, SearchItem, SearchPage


class AudioReader(Protocol):
    """Доступ к одной записи без HTTP-объекта хранилища."""

    async def open(self, query: AudioQuery) -> RecordingStream:
        """Загрузить метаданные, открыть поток и журналировать явное скачивание."""
        ...


class ExportRepository(Protocol):
    """Согласованные данные поиска и факт их экспорта в одной сессии."""

    async def search(
        self, query: str, filters: SearchFilters, *, limit: int, offset: int
    ) -> SearchPage:
        """Получить очередную страницу поисковой выборки."""
        ...

    async def record_export(self, event: ExportAudit) -> None:
        """Атомарно зафиксировать факт экспорта после чтения результатов."""
        ...


class CsvRenderer(Protocol):
    """Формирование файла без зависимости сценария от CSV-кодека."""

    def render(self, rows: Sequence[SearchItem], source: CallSource) -> FileResult:
        """Преобразовать готовые результаты в CSV с безопасными ячейками."""
        ...
