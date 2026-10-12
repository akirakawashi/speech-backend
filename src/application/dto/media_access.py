"""Команды доступа к записи и экспорта, независимые от FastAPI и SQL."""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.application.authorization import Principal
from src.application.dto.calls import CallSource, Product, Segment
from src.application.dto.search import SearchMode

EXPORT_LIMIT = 10_000
CSV_DELIMITER = ";"
CSV_BOM = "\ufeff"


class AudioQuery(BaseModel):
    """Идентификатор записи, личность и необязательный диапазон байтов."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    call_id: UUID
    byte_range: str | None = None
    download: bool = False


class RecordingStream(BaseModel):
    """Поток байтов записи и метаданные диапазона; завершение освобождает ресурс."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    chunks: AsyncIterator[bytes] = Field(repr=False)
    close: Callable[[], Awaitable[None]] = Field(repr=False)
    partial: bool = False
    range_unsatisfiable: bool = False
    content_type: str = "audio/mpeg"
    content_range: str | None = None
    content_length: int | None = None
    download_name: str | None = None


class ExportQuery(BaseModel):
    """Отбор экспорта результатов с теми же параметрами, что у поиска."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    q: str = ""
    date_from: datetime | None = None
    date_to: datetime | None = None
    period: str | None = None
    operator_id: UUID | None = None
    project_id: UUID | None = None
    node_id: UUID | None = None
    segment: Segment | None = None
    product: Product | None = None
    source: CallSource = CallSource.NCC


class ExportAudit(BaseModel):
    """Количество выгруженных записей и отбор без самого текста разговора."""

    model_config = ConfigDict(frozen=True)
    actor: Principal
    rows: int
    query_length: int
    mode: SearchMode
    source: str
