"""Правила расписания приёма и постановки оценки в общую очередь."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CollectionSchedule(BaseModel):
    """Сохранить интервалы, дневную долю NCC и ночное окно Телфина."""

    model_config = ConfigDict(frozen=True)
    timezone: str = "Europe/Moscow"
    ncc_interval_seconds: int = Field(default=900, ge=60, le=86400)
    telphin_interval_seconds: int = Field(default=3600, ge=60, le=86400)
    quality_interval_seconds: int = Field(default=900, ge=60, le=86400)
    immediate_share: float = Field(default=0.5, ge=0, le=1)
    night_from: int = Field(default=19, ge=0, le=23)
    night_until: int = Field(default=7, ge=0, le=23)
    quality_limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def valid_window(self) -> "CollectionSchedule":
        """Проверить часовой пояс и запретить пустое ночное окно."""
        ZoneInfo(self.timezone)
        if self.night_from == self.night_until:
            raise ValueError("night window cannot be empty")
        return self

    def next_run(
        self, action: Literal["ncc", "telphin", "quality"], now: datetime, last: datetime | None
    ) -> datetime:
        """Вернуть ближайший запуск с привязкой к границам интервала.

        При старте доступное действие выполняется сразу. После простоя делаем
        один догон вместо отдельного запуска на каждый пропущенный интервал.
        Телфин принимается только в ночном окне; граница конца исключается.
        """
        interval = {
            "ncc": self.ncc_interval_seconds,
            "telphin": self.telphin_interval_seconds,
            "quality": self.quality_interval_seconds,
        }[action]
        zone = ZoneInfo(self.timezone)
        local = now.astimezone(zone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = (local - midnight).total_seconds()
        boundary = midnight + timedelta(seconds=int(elapsed // interval) * interval)
        candidate = (
            now if last is None or last < boundary else boundary + timedelta(seconds=interval)
        )
        if action == "telphin":
            candidate = self._next_night(candidate)
        return candidate.astimezone(UTC)

    def _next_night(self, candidate: datetime) -> datetime:
        """Оставить время внутри ночи либо вернуть ближайшее начало разрешённого окна."""
        local = candidate.astimezone(ZoneInfo(self.timezone))
        hour = local.hour
        inside = (
            hour >= self.night_from or hour < self.night_until
            if self.night_from > self.night_until
            else self.night_from <= hour < self.night_until
        )
        if inside:
            return local
        start = local.replace(hour=self.night_from, minute=0, second=0, microsecond=0)
        return start if start >= local else start + timedelta(days=1)
