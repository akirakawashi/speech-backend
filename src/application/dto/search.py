"""Отбор и результаты поиска разговоров без SQL и HTTP-объектов."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.domain.calls.contracts import CallSource

PAGE_SIZE: Final[int] = 20


MAX_PAGE_SIZE: Final[int] = 100


SearchMode = Literal["phrase", "any_order", "browse"]


def parse_mode(query: str) -> SearchMode:
    """Как пользователю подписать режим поиска (SRC-01).

    `websearch_to_tsquery` сам понимает кавычки как фразовый оператор, поэтому
    отдельной ветки разбора нет — есть только подпись. Отличать режимы своим
    разбором значило бы завести второй синтаксис рядом с настоящим и однажды
    разойтись с ним.
    """
    stripped = query.strip()
    if not stripped:
        return "browse"
    return "phrase" if '"' in stripped else "any_order"


class SearchFilters(BaseModel):
    """Чем сужается выдача. Пустое поле — «не сужать»."""

    model_config = ConfigDict(frozen=True)

    date_from: datetime | None = None
    date_to: datetime | None = None
    operator_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    #: Узел классификатора (SP-310): звонки, которые ИИ классифицировал этим
    #: узлом или любым под ним.
    node_id: uuid.UUID | None = None
    #: Сегмент клиента и линейка продукта (решение S-21). Фильтруют по
    #: значению, а не по источнику: пользователю важно «звонки юрлиц», а не
    #: «звонки юрлиц, определённые проектом».
    segment: str | None = None
    product: str | None = None
    #: Источник звонков (S-35): входящие NCC или исходящие Телфина. Здесь, как и
    #: у остальных полей, пустое значение — «не сужать»; решение, что экран без
    #: параметра показывает только NCC, принимает ручка, а не поисковый слой.
    source: str | None = None


class Quote(BaseModel):
    """Цитата с таймкодом для перемотки."""

    model_config = ConfigDict(frozen=True)

    seq: int
    start_ms: int
    speaker: str
    text: str


class SearchItem(BaseModel):
    """Карточка результата (SRC-04)."""

    model_config = ConfigDict(frozen=True)

    call_id: uuid.UUID
    session_id: str
    started_at: datetime
    duration_s: int
    match_count: int
    operator_login: str | None
    project_title: str | None
    sentiment: str | None
    audio_status: str
    processing_status: str
    segment: str | None = None
    segment_source: str = "unknown"
    product: str | None = None
    product_source: str = "unknown"
    #: Источник звонка (S-35): по нему карточка подписывает, чей это звонок.
    source: str = CallSource.NCC.value
    #: Классификация ИИ (SP-310): узел и названия уровней от корня.
    ai_node_id: uuid.UUID | None = None
    ai_path: tuple[str, ...] = ()
    quotes: tuple[Quote, ...] = ()
    hit_seqs: tuple[int, ...] = ()


class SearchPage(BaseModel):
    """Страница выдачи вместе со счётчиками над ней."""

    model_config = ConfigDict(frozen=True)

    mode: SearchMode
    total_matches: int
    total_calls: int
    period_calls: int
    items: tuple[SearchItem, ...] = Field(default_factory=tuple)

    @property
    def share_of_period(self) -> float:
        """Доля найденных разговоров от всех звонков периода (SRC-03).

        Ноль при пустом периоде, а не деление на ноль: «0%» — осмысленный
        ответ, исключение на пустой выборке — нет.
        """
        if self.period_calls <= 0:
            return 0.0
        return self.total_calls / self.period_calls
