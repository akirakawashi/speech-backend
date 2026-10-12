"""Результаты служебных сценариев без зависимости от базы и HTTP."""

from pydantic import BaseModel, ConfigDict


class VersionInfo(BaseModel):
    """The `/version` payload. Frozen: a caller may cache it for the process."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    service: str
    commit: str
    build_time: str
    contracts: dict[str, str]


class Readiness(BaseModel):
    """Достижимость базы и соответствие ожидаемой схеме приложения."""

    model_config = ConfigDict(frozen=True)
    ready: bool
    detail: str | None = None


class MetricsText(BaseModel):
    """Собранные метрики в формате сбора процессных счётчиков."""

    model_config = ConfigDict(frozen=True)
    content: str
