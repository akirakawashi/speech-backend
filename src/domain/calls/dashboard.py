"""Правила группировки обращений, долей негатива и динамики периода."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.domain.calls.classifier import TOPIC_LEVEL, NodePaths, label_key, present_levels

Grain = Literal["day", "week", "month"]


TOP_THEMES: Final[int] = 6


OTHER_THEME: Final[str] = "__other__"


OTHER_LABEL: Final[str] = "Прочее"


NO_SUBTOPIC_LABEL: Final[str] = "Без уточнения"


AUTO_DAY_LIMIT: Final[int] = 10


AUTO_WEEK_LIMIT: Final[int] = 120


MAX_BUCKETS: Final[int] = 100


class DayCount(BaseModel):
    """Звонки одного дня по одной теме и подтеме."""

    model_config = ConfigDict(frozen=True)

    day: date
    theme: str
    theme_label: str
    #: Ключ подтемы; пусто — модель дошла до темы и не уточнила.
    subtopic: str
    subtopic_label: str
    calls: int
    negative: int
    #: Звонки с тональностью: знаменатель доли негатива.
    rated: int


class _Tally(BaseModel):
    """Накапливать число обращений и негативных разговоров одной группы."""

    model_config = ConfigDict(frozen=False)

    calls: int = 0
    negative: int = 0
    rated: int = 0

    def add(self, row: DayCount) -> None:
        """Добавить результат в накопленные счётчики группы."""
        self.calls += row.calls
        self.negative += row.negative
        self.rated += row.rated

    @property
    def negative_share(self) -> float | None:
        """Вернуть долю негативных разговоров; пустой группе соответствует ноль."""
        return _share(self.negative, self.rated) if self.rated else None


class Theme(BaseModel):
    """Тема дашборда и агрегаты обращений по ней."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    calls: int
    share: float
    negative_share: float | None


class Bucket(BaseModel):
    """Календарная корзина дашборда с итогом тем и тональности."""

    model_config = ConfigDict(frozen=True)

    start: date
    end: date
    calls: int
    #: Звонки по темам — в порядке `Dashboard.themes`.
    by_theme: tuple[int, ...]
    negative_share: float | None


class Subtopic(BaseModel):
    """Итог обращений и тональности по подтеме."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    calls: int
    share: float


class Peak(BaseModel):
    """Календарный максимум обращений по выбранной теме."""

    model_config = ConfigDict(frozen=True)

    start: date
    end: date
    calls: int


class Selection(BaseModel):
    """Числа выбранной темы — или всех тем, если тема не выбрана."""

    model_config = ConfigDict(frozen=True)

    theme: str | None
    label: str | None
    calls: int
    negative_share: float | None
    #: Вторая половина периода к первой, долей: 0.12 — на 12% больше. `None` —
    #: сравнивать не с чем: период в один день или в первой половине пусто.
    change: float | None
    peak: Peak | None
    subtopics: tuple[Subtopic, ...] = ()


class Dashboard(BaseModel):
    """Результаты расчёта обращений, тем и негатива за период."""

    model_config = ConfigDict(frozen=True)

    first_day: date
    last_day: date
    grain: Grain
    calls: int
    themes: tuple[Theme, ...]
    buckets: tuple[Bucket, ...]
    selection: Selection
    #: Все темы периода по отдельности, без «Прочего» — для выгрузки.
    all_themes: tuple[Theme, ...] = Field(default=())
    #: Подтемы каждой темы — для выгрузки.
    subtopics_by_theme: Mapping[str, tuple[Subtopic, ...]] = Field(default_factory=dict)


def _share(part: int, whole: int) -> float:
    """Вычислить долю; при пустом охвате вернуть ноль."""
    return round(part / whole, 4) if whole else 0.0


def auto_grain(days: int) -> Grain:
    """Выбрать календарную детализацию дашборда по длине периода."""
    if days <= AUTO_DAY_LIMIT:
        return "day"
    return "week" if days <= AUTO_WEEK_LIMIT else "month"


def _bucket_start(day: date, grain: Grain) -> date:
    """Вычислить начало календарной корзины заданной детализации."""
    if grain == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1) if grain == "month" else day


def _bucket_end(start: date, grain: Grain) -> date:
    """Вычислить конец календарной корзины заданной детализации."""
    if grain == "week":
        return start + timedelta(days=6)
    if grain == "month":
        following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return following - timedelta(days=1)
    return start


def bucket_spans(first: date, last: date, grain: Grain) -> list[tuple[date, date]]:
    """Корзины периода по календарю, обе границы включительно.

    Неделя — с понедельника. Крайние корзины обрезаны периодом: неделя, в
    которую попал один его день, — это один день, а не семь.
    """
    spans: list[tuple[date, date]] = []
    start = _bucket_start(first, grain)
    while start <= last:
        end = _bucket_end(start, grain)
        spans.append((max(start, first), min(end, last)))
        start = end + timedelta(days=1)
    return spans


def resolve_grain(first: date, last: date, asked: Grain | None) -> Grain:
    """Корзина запроса: названная, если график её вместит, иначе автоматическая."""
    auto = auto_grain((last - first).days + 1)
    if asked is None or len(bucket_spans(first, last, asked)) > MAX_BUCKETS:
        return auto
    return asked


def _named(label: str) -> bool:
    """Название ли это. Пустой уровень и прочерк — «уровня нет», как во всём классификаторе."""
    return present_levels((label,))[0]


def day_counts(rows: Iterable[Mapping[Any, Any]], paths: NodePaths) -> list[DayCount]:
    """Строки запроса — в счёты по темам. Узел без уровня темы пропускается.

    Уровень без названия — не тема и не подтема. В дереве заказчика таких нет,
    но дерево пополняется путями операторов, а они ставят прочерк там, где
    уточнения нет: «тема "-"» на графике была бы строкой ни о чём.
    """
    found: list[DayCount] = []
    for row in rows:
        node_id = row["node_id"]
        if not isinstance(node_id, uuid.UUID):
            continue
        theme_node = paths.ancestor(node_id, TOPIC_LEVEL)
        if theme_node is None:
            continue
        theme_label = str(paths.node(theme_node)["label"])
        if not _named(theme_label):
            continue
        detail = paths.ancestor(node_id, TOPIC_LEVEL + 1)
        detail_label = str(paths.node(detail)["label"]) if detail is not None else ""
        if not _named(detail_label):
            detail_label = ""
        found.append(
            DayCount(
                day=row["day"],
                theme=label_key(theme_label),
                theme_label=theme_label,
                subtopic=label_key(detail_label),
                subtopic_label=detail_label,
                calls=int(row["calls"]),
                negative=int(row["negative"]),
                rated=int(row["rated"]),
            )
        )
    return found


def theme_labels(paths: NodePaths, node_ids: Iterable[uuid.UUID]) -> dict[str, str]:
    """Названия тем по ключу — для темы из ссылки, которой в периоде не оказалось."""
    labels: dict[str, str] = {}
    for node_id in node_ids:
        node = paths.node(node_id)
        if int(node["level"]) == TOPIC_LEVEL:
            labels.setdefault(label_key(node["label"]), str(node["label"]))
    return labels


def _halves(first: date, last: date) -> tuple[date, date] | None:
    """Конец первой половины периода и начало второй; половины равной длины.

    В периоде из нечётного числа дней средний день не входит ни в одну: иначе
    одна половина была бы длиннее, и «выросло» значило бы «в ней на день больше».
    """
    days = (last - first).days + 1
    half = days // 2
    if half == 0:
        return None
    return first + timedelta(days=half - 1), last - timedelta(days=half - 1)


def _subtopics(rows: Sequence[DayCount]) -> tuple[Subtopic, ...]:
    """Подтемы набора строк по убыванию. «Без уточнения» — последней."""
    calls: dict[str, int] = defaultdict(int)
    labels: dict[str, str] = {}
    for row in rows:
        calls[row.subtopic] += row.calls
        labels.setdefault(row.subtopic, row.subtopic_label or NO_SUBTOPIC_LABEL)
    total = sum(calls.values())
    # Тема, у которой уточнений нет вовсе, подтем не имеет: строка «Без
    # уточнения — 100%» ничего бы не сообщила.
    if set(calls) <= {""}:
        return ()
    ordered = sorted(calls, key=lambda key: (key == "", -calls[key], labels[key]))
    return tuple(
        Subtopic(key=key, label=labels[key], calls=calls[key], share=_share(calls[key], total))
        for key in ordered
    )


def _selection(
    rows: Sequence[DayCount],
    *,
    theme: str | None,
    label: str | None,
    first: date,
    last: date,
    subtopics: tuple[Subtopic, ...],
) -> Selection:
    """Выбрать данные темы и подтем для заданного охвата дашборда."""
    total = _Tally()
    by_day: dict[date, int] = defaultdict(int)
    for row in rows:
        total.add(row)
        by_day[row.day] += row.calls

    change: float | None = None
    halves = _halves(first, last)
    if halves is not None:
        first_end, second_start = halves
        before = sum(calls for day, calls in by_day.items() if day <= first_end)
        after = sum(calls for day, calls in by_day.items() if day >= second_start)
        if before:
            change = round((after - before) / before, 4)

    peak: Peak | None = None
    for start, end in bucket_spans(first, last, "week"):
        calls = sum(by_day.get(start + timedelta(days=n), 0) for n in range((end - start).days + 1))
        # При равенстве — более ранняя неделя: строгое «больше».
        if calls and (peak is None or calls > peak.calls):
            peak = Peak(start=start, end=end, calls=calls)

    return Selection(
        theme=theme,
        label=label,
        calls=total.calls,
        negative_share=total.negative_share,
        change=change,
        peak=peak,
        subtopics=subtopics,
    )


def build(
    rows: Sequence[DayCount],
    *,
    first: date,
    last: date,
    grain: Grain | None = None,
    theme: str | None = None,
    known_labels: Mapping[str, str] | None = None,
) -> Dashboard:
    """Собрать дашборд периода `first … last` (оба дня включительно).

    `theme` — ключ выбранной темы или `OTHER_THEME`; `None` — все темы. Тема,
    которой в периоде нет, — не ошибка: ссылка с выбранной темой переживает
    смену периода, и в ответе у неё нули. `known_labels` — названия тем
    классификатора для такого случая.
    """
    rows = [row for row in rows if first <= row.day <= last]
    used = resolve_grain(first, last, grain)

    tallies: dict[str, _Tally] = defaultdict(_Tally)
    labels: dict[str, str] = {}
    for row in rows:
        tallies[row.theme].add(row)
        labels.setdefault(row.theme, row.theme_label)
    total = sum(tally.calls for tally in tallies.values())
    ranked = sorted(tallies, key=lambda key: (-tallies[key].calls, labels[key]))
    all_themes = tuple(
        Theme(
            key=key,
            label=labels[key],
            calls=tallies[key].calls,
            share=_share(tallies[key].calls, total),
            negative_share=tallies[key].negative_share,
        )
        for key in ranked
    )

    # Шесть тем и «Прочее»; ровно семь тем показываются все под своими именами.
    shown = ranked if len(ranked) <= TOP_THEMES + 1 else ranked[:TOP_THEMES]
    rest = [key for key in ranked if key not in shown]
    themes = [theme_ for theme_ in all_themes if theme_.key in shown]
    if rest:
        other = _Tally()
        for row in rows:
            if row.theme in rest:
                other.add(row)
        themes.append(
            Theme(
                key=OTHER_THEME,
                label=OTHER_LABEL,
                calls=other.calls,
                share=_share(other.calls, total),
                negative_share=other.negative_share,
            )
        )
    column = {key: index for index, key in enumerate(shown)}
    other_column = len(shown) if rest else None

    spans = bucket_spans(first, last, used)
    starts = [start for start, _ in spans]
    per_bucket: list[list[int]] = [[0] * len(themes) for _ in spans]
    bucket_tally = [_Tally() for _ in spans]
    index_of = {start: index for index, start in enumerate(starts)}
    for row in rows:
        index = index_of[max(_bucket_start(row.day, used), first)]
        position = column.get(row.theme, other_column)
        assert position is not None
        per_bucket[index][position] += row.calls
        bucket_tally[index].add(row)
    buckets = tuple(
        Bucket(
            start=start,
            end=end,
            calls=bucket_tally[index].calls,
            by_theme=tuple(per_bucket[index]),
            negative_share=bucket_tally[index].negative_share,
        )
        for index, (start, end) in enumerate(spans)
    )

    by_theme_rows: dict[str, list[DayCount]] = defaultdict(list)
    for row in rows:
        by_theme_rows[row.theme].append(row)
    subtopics_by_theme = {key: _subtopics(by_theme_rows[key]) for key in ranked}

    if theme is None:
        selection = _selection(rows, theme=None, label=None, first=first, last=last, subtopics=())
    elif theme == OTHER_THEME:
        # Подтемы «Прочего» — темы, которые в него сложены.
        inside = [item for item in all_themes if item.key in rest]
        whole = sum(item.calls for item in inside)
        selection = _selection(
            [row for row in rows if row.theme in rest],
            theme=OTHER_THEME,
            label=OTHER_LABEL,
            first=first,
            last=last,
            subtopics=tuple(
                Subtopic(
                    key=item.key,
                    label=item.label,
                    calls=item.calls,
                    share=_share(item.calls, whole),
                )
                for item in inside
            ),
        )
    else:
        selection = _selection(
            by_theme_rows.get(theme, []),
            theme=theme,
            label=labels.get(theme) or (known_labels or {}).get(theme) or theme,
            first=first,
            last=last,
            subtopics=subtopics_by_theme.get(theme, ()),
        )

    return Dashboard(
        first_day=first,
        last_day=last,
        grain=used,
        calls=total,
        themes=tuple(themes),
        buckets=buckets,
        selection=selection,
        all_themes=all_themes,
        subtopics_by_theme=subtopics_by_theme,
    )


__all__ = [
    "MAX_BUCKETS",
    "NO_SUBTOPIC_LABEL",
    "OTHER_LABEL",
    "OTHER_THEME",
    "TOP_THEMES",
    "Bucket",
    "Dashboard",
    "DayCount",
    "Grain",
    "Peak",
    "Selection",
    "Subtopic",
    "Theme",
    "auto_grain",
    "bucket_spans",
    "build",
    "day_counts",
    "resolve_grain",
    "theme_labels",
]
