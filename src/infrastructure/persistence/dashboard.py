"""Дашборд: обращения по темам во времени и доля негатива.

Считается по тому, что разбор звонка уже записал: узел классификатора
(`ai_node_id`), тональность (`sentiment`) и время начала. Своих данных у
дашборда нет, и модель он не вызывает.

**Тема** — уровень 4 классификатора (`TOPIC_LEVEL`), а не узел: «Не работает»
у физлица на ШПД и у юрлица на мобильной связи — четыре разных узла и одна тема.
Темы складываются по ключу названия (`label_key`). **Подтема** — уровень 5 под
любым из узлов темы; звонок, которому модель уточнения не дала, идёт в «Без
уточнения».

На графике — шесть самых частых тем периода и «Прочее» (решение владельца
2026-10-08): столбец из одиннадцати цветов не читается. Если тем всего семь,
седьмая идёт под своим именем — прятать одну тему под «Прочее» незачем.

Звонок без темы — не разобранный, либо с путём, не дошедшим до уровня темы, — в
дашборд не входит: показывать его нечем. Сколько звонков всего и сколько из
них расшифровано, отвечает `/api/v1/stats`.

Сборка — чистая функция над суточными счётами (`build`): запрос отдаёт «день,
узел, сколько звонков», а корзины, доли, половины периода и пик недели
считаются здесь и проверяются без базы.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.dashboard import (
    AUTO_DAY_LIMIT as AUTO_DAY_LIMIT,
    AUTO_WEEK_LIMIT as AUTO_WEEK_LIMIT,
    MAX_BUCKETS as MAX_BUCKETS,
    NO_SUBTOPIC_LABEL as NO_SUBTOPIC_LABEL,
    OTHER_LABEL as OTHER_LABEL,
    OTHER_THEME as OTHER_THEME,
    TOP_THEMES as TOP_THEMES,
    Bucket as Bucket,
    Dashboard as Dashboard,
    DayCount as DayCount,
    Grain as Grain,
    Peak as Peak,
    Selection as Selection,
    Subtopic as Subtopic,
    Theme as Theme,
    __all__ as __all__,
    _bucket_end as _bucket_end,
    _bucket_start as _bucket_start,
    _halves as _halves,
    _named as _named,
    _selection as _selection,
    _share as _share,
    _subtopics as _subtopics,
    _Tally as _Tally,
    auto_grain as auto_grain,
    bucket_spans as bucket_spans,
    build as build,
    day_counts as day_counts,
    resolve_grain as resolve_grain,
    theme_labels as theme_labels,
)
from src.infrastructure.persistence.classifier import (
    NodePaths,
)

#: Сколько тем показывается на графике под своим именем; остальные — «Прочее».

#: Ключ «Прочего». Совпасть с ключом настоящей темы он не может: `label_key`
#: заменяет подчёркивания косой чертой.

#: Подтема звонка, у которого модель дошла до темы и дальше не уточнила.

#: Автовыбор корзины по длине периода — из макета: до десяти дней — дни, до
#: ста двадцати — недели, дальше — месяцы.

#: Больше столбцов график не показывает. Ручной выбор корзины, который дал бы
#: больше, заменяется автоматическим: год по дням — 365 столбцов шириной в точку.

#: Суточные счёты по узлам. Сутки — по часам колл-центра: звонок в 23:30 по
#: Москве принадлежит своему дню, а не следующему по UTC.
_COUNTS_SQL = text(
    """
    SELECT CAST(c.started_at AT TIME ZONE :tz AS date) AS day,
           c.ai_node_id AS node_id,
           count(*) AS calls,
           count(*) FILTER (WHERE c.sentiment = 'negative') AS negative,
           count(*) FILTER (WHERE c.sentiment IS NOT NULL) AS rated
      FROM speech_calls AS c
     WHERE c.source = :source
       AND c.ai_node_id IS NOT NULL
       AND (CAST(:date_from AS timestamptz) IS NULL OR c.started_at >= :date_from)
       AND (CAST(:date_to   AS timestamptz) IS NULL OR c.started_at <  :date_to)
     GROUP BY 1, 2
    """
)


async def load_counts(
    session: AsyncSession,
    *,
    source: str,
    date_from: datetime | None,
    date_to: datetime | None,
    timezone: str,
) -> tuple[list[DayCount], dict[str, str]]:
    """Суточные счёты периода и названия всех тем классификатора."""
    rows = (
        (
            await session.execute(
                _COUNTS_SQL,
                {"source": source, "date_from": date_from, "date_to": date_to, "tz": timezone},
            )
        )
        .mappings()
        .all()
    )
    paths = await NodePaths.load(session)
    return day_counts(rows, paths), theme_labels(paths, paths.ids())
