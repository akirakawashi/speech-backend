"""Общий ввод периода для сценариев чтения звонков."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PeriodQuery(BaseModel):
    """Полуоткрытые границы периода либо явный выбор всей истории через period."""

    model_config = ConfigDict(frozen=True)
    date_from: datetime | None = None
    date_to: datetime | None = None
    period: str | None = None
