"""Разрешение явно заданного периода и умолчаний сценария поиска."""

from datetime import UTC, datetime, timedelta
from typing import Final

from src.application.dto.period import PeriodQuery

DEFAULT_PERIOD_DAYS = 30

TOP_TOPICS = 10


def resolve_period(
    date_from: datetime | None, date_to: datetime | None, *, all_time: bool = False
) -> tuple[datetime | None, datetime | None]:
    """Период запроса (SRC-02).

    Три случая, и их пришлось развести явно:

    * **обе границы пусты** — умолчание в тридцать дней. Запрос без параметров
      иначе прошёл бы по всему архиву, и заметили бы это на приёмке, когда
      архива станет полгода;
    * **задана одна граница** — вторая не достраивается: пользователь выразил
      намерение, и достраивать за него значит менять смысл запроса;
    * **`all_time`** — период снят совсем.

    Третий случай появился не сразу, и это была ошибка. Кнопка «Искать за всё
    время» снимала период на клиенте, запрос уходил без границ — и сервер
    подставлял те же тридцать дней. Кнопка нажималась, ничего не менялось, и
    объяснить это по экрану было нельзя. «Всё время» невыразимо отсутствием
    параметров, потому что отсутствие уже занято умолчанием: его надо
    называть.

    Растянуть период на век вместо этого нельзя: сервер считает долю найденного
    от периода (SRC-03), и век превратил бы её в ноль.
    """
    if all_time:
        return (None, None)
    if date_from is None and date_to is None:
        now = datetime.now(UTC)
        return (now - timedelta(days=DEFAULT_PERIOD_DAYS), None)
    return (date_from, date_to)


PERIOD_ALL: Final[str] = "all"


def is_all_time(period: str | None) -> bool:
    """Проверить явный выбор поиска за всё время."""
    return bool((period or "").strip().lower() == PERIOD_ALL)


def normalized_period[Query: PeriodQuery](query: Query) -> Query:
    """Разрешить умолчания периода DTO до вызова хранилища; вернуть копию с границами."""
    start, end = resolve_period(query.date_from, query.date_to, all_time=is_all_time(query.period))
    return query.model_copy(update={"date_from": start, "date_to": end, "period": None})
