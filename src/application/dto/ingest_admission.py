"""Внутренние снимки правил получения записи; в HTTP-команду пороги не входят."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from src.application.dto.pipeline_input import CallForIngest


class IngestAdmission(BaseModel):
    """Решение по настройкам на начало конкретного захвата; без URL и номера клиента."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    claim_token: UUID
    skip_short: bool
    object_key: str
    segment: str | None = None
    product: str | None = None


class IngestContext(BaseModel):
    """Данные живого задания и сохранённое решение; только для сценария бэкенда."""

    model_config = ConfigDict(frozen=True)
    call: CallForIngest
    max_attempts: int
    short_call_threshold_s: int
    admission: IngestAdmission | None = None
