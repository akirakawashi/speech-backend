"""Поиск по разговорам.

Три запроса на выдачу, и такое разделение выбрано намеренно.

Первый даёт **и счётчики, и страницу**: «найдено N совпадений в M разговорах» и
доля M от всех звонков периода (SRC-03) считаются тем же проходом по индексу,
что и сама страница. Считать их отдельными запросами значило бы пройти по
`speech_transcript_segments` дважды и получить счётчик, который может не
сойтись со списком под ним.

Второй достаёт цитаты — но только для двадцати звонков страницы. `ts_headline`
разбирает текст заново для каждой строки, и звать его на все найденные реплики
архива значило бы платить за то, чего никто не увидит.

Третий отдаёт номера реплик с попаданиями: из них строятся метки на полосе
записи и переходы «пред./след.» (PLY-03).

**Ищем по тексту расшифровки** (`text_raw`): индекс построен по нему, и другого
текста у реплики нет.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.search import (
    MAX_PAGE_SIZE as MAX_PAGE_SIZE,
    PAGE_SIZE as PAGE_SIZE,
    Quote as Quote,
    SearchFilters as SearchFilters,
    SearchItem as SearchItem,
    SearchMode as SearchMode,
    SearchPage as SearchPage,
    parse_mode as parse_mode,
)
from src.infrastructure.persistence.classifier import NodePaths, branch_sql

#: Русская конфигурация словаря. Одна на весь модуль и на генерируемую колонку
#: `search_vector`: разойдись они, индекс перестал бы отвечать на собственные
#: запросы — молча, потому что синтаксически всё верно.
FTS_CONFIG: Final[str] = "russian"

#: Сколько цитат показывает карточка результата (SRC-04).
QUOTES_PER_CALL: Final[int] = 2

#: Размер страницы по умолчанию (SRC-05).


#: Чем `ts_headline` помечает найденное. Управляющие символы, а не «ёлочки»:
#: расшифровка русской речи содержит кавычки-ёлочки сама по себе («Ромашка»,
#: «Кошелёк»), и по такой метке интерфейс подсветил бы название компании как
#: совпадение. Отличить свою метку от чужой кавычки было бы нечем.
#:
#: STX и ETX (U+0002, U+0003) в тексте разговора появиться не могут: их нет ни
#: в выводе Whisper. Наружу они не уходят — фронт режет по ним
#: строку и оборачивает найденное в `<mark>`, а разметку HTML сервер не отдаёт
#: вовсе, чтобы не заводить путь, на котором её пришлось бы вставлять как есть.
HIGHLIGHT_START: Final[str] = "\x02"
HIGHLIGHT_STOP: Final[str] = "\x03"

_HEADLINE_OPTIONS: Final[str] = (
    f"StartSel={HIGHLIGHT_START},StopSel={HIGHLIGHT_STOP},MaxWords=18,MinWords=8,MaxFragments=1"
)


#: Условия фильтров вынесены в один фрагмент: они одинаковы в поиске и в
#: подсчёте звонков периода, и разъехавшись, дали бы долю больше единицы.
#:
#: Источник (S-35) стоит здесь же, а не отдельным условием у выдачи: иначе доля
#: найденного Телфина считалась бы от всех звонков периода вместе с NCC и была
#: бы занижена ровно на весь поток NCC.
_FILTERS_SQL = f"""
           (CAST(:source AS text) IS NULL OR c.source = CAST(:source AS text))
       AND (CAST(:date_from AS timestamptz) IS NULL OR c.started_at >= :date_from)
       AND (CAST(:date_to   AS timestamptz) IS NULL OR c.started_at <  :date_to)
       AND (CAST(:operator_id AS uuid) IS NULL OR c.operator_id = CAST(:operator_id AS uuid))
       AND (CAST(:project_id  AS uuid) IS NULL OR c.project_id  = CAST(:project_id  AS uuid))
       AND {branch_sql("c.ai_node_id", "node_id")}
       AND (CAST(:segment AS text) IS NULL OR c.segment = CAST(:segment AS text))
       AND (CAST(:product AS text) IS NULL OR c.product = CAST(:product AS text))
"""

_SEARCH_SQL = text(
    f"""
    WITH q AS (SELECT websearch_to_tsquery('{FTS_CONFIG}', :query) AS tsq),
    filtered AS (
        SELECT c.id, c.started_at
          FROM speech_calls AS c
         WHERE {_FILTERS_SQL}
    ),
    hits AS (
        SELECT s.call_id, count(*) AS match_count
          FROM speech_transcript_segments AS s
          JOIN filtered AS f ON f.id = s.call_id
         CROSS JOIN q
         WHERE s.search_vector @@ q.tsq
         GROUP BY s.call_id
    )
    SELECT (SELECT coalesce(sum(match_count), 0) FROM hits) AS total_matches,
           (SELECT count(*) FROM hits)                      AS total_calls,
           (SELECT count(*) FROM filtered)                  AS period_calls,
           h.match_count,
           c.id AS call_id, c.session_id, c.started_at, c.duration_s,
           c.sentiment, c.audio_status, c.processing_status,
           c.segment, c.segment_source, c.product, c.product_source, c.ai_node_id,
           c.source,
           o.login AS operator_login, p.title AS project_title
      FROM hits AS h
      JOIN speech_calls AS c ON c.id = h.call_id
      LEFT JOIN speech_operators AS o ON o.id = c.operator_id
      LEFT JOIN speech_ncc_projects AS p ON p.id = c.project_id
     ORDER BY c.started_at DESC, c.id DESC
     LIMIT :limit OFFSET :offset
    """
)

#: Просмотр без поискового слова. Отдельный запрос, а не тот же с пустым
#: `tsquery`: пустой запрос не совпадает ни с чем, и выдача была бы пустой там,
#: где пользователь всего лишь сузил период или тему.
_BROWSE_SQL = text(
    f"""
    WITH filtered AS (
        SELECT c.id FROM speech_calls AS c WHERE {_FILTERS_SQL}
    )
    SELECT 0 AS total_matches,
           (SELECT count(*) FROM filtered) AS total_calls,
           (SELECT count(*) FROM filtered) AS period_calls,
           0 AS match_count,
           c.id AS call_id, c.session_id, c.started_at, c.duration_s,
           c.sentiment, c.audio_status, c.processing_status,
           c.segment, c.segment_source, c.product, c.product_source, c.ai_node_id,
           c.source,
           o.login AS operator_login, p.title AS project_title
      FROM filtered AS f
      JOIN speech_calls AS c ON c.id = f.id
      LEFT JOIN speech_operators AS o ON o.id = c.operator_id
      LEFT JOIN speech_ncc_projects AS p ON p.id = c.project_id
     ORDER BY c.started_at DESC, c.id DESC
     LIMIT :limit OFFSET :offset
    """
)

#: Цитаты только для звонков страницы. `ts_headline` разбирает текст заново на
#: каждую строку — звать его на весь архив значило бы платить за то, чего никто
#: не увидит.
_QUOTES_SQL = text(
    f"""
    WITH q AS (SELECT websearch_to_tsquery('{FTS_CONFIG}', :query) AS tsq),
    matched AS (
        SELECT s.call_id, s.seq, s.start_ms, s.speaker, s.text_raw,
               row_number() OVER (PARTITION BY s.call_id ORDER BY s.seq) AS rank
          FROM speech_transcript_segments AS s
         CROSS JOIN q
         WHERE s.call_id = ANY(CAST(:call_ids AS uuid[]))
           AND s.search_vector @@ q.tsq
    )
    SELECT m.call_id, m.seq, m.start_ms, m.speaker,
           ts_headline('{FTS_CONFIG}', m.text_raw, q.tsq, '{_HEADLINE_OPTIONS}') AS quote
      FROM matched AS m
     CROSS JOIN q
     WHERE m.rank <= :quotes
     ORDER BY m.call_id, m.seq
    """
)

#: Все номера реплик с попаданиями — для меток на полосе записи (PLY-03).
_HIT_SEQS_SQL = text(
    f"""
    SELECT s.call_id, s.seq
      FROM speech_transcript_segments AS s
     CROSS JOIN (SELECT websearch_to_tsquery('{FTS_CONFIG}', :query) AS tsq) AS q
     WHERE s.call_id = ANY(CAST(:call_ids AS uuid[]))
       AND s.search_vector @@ q.tsq
     ORDER BY s.call_id, s.seq
    """
)


async def search_calls(
    session: AsyncSession,
    *,
    query: str,
    filters: SearchFilters | None = None,
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> SearchPage:
    """Найти разговоры и собрать страницу выдачи целиком."""
    mode = parse_mode(query)
    narrowed = filters or SearchFilters()
    params: dict[str, Any] = {
        "query": query.strip(),
        "date_from": narrowed.date_from,
        "date_to": narrowed.date_to,
        "operator_id": narrowed.operator_id,
        "project_id": narrowed.project_id,
        "node_id": narrowed.node_id,
        "segment": narrowed.segment,
        "product": narrowed.product,
        "source": narrowed.source,
        "limit": max(1, min(limit, MAX_PAGE_SIZE)),
        "offset": max(0, offset),
    }

    statement = _BROWSE_SQL if mode == "browse" else _SEARCH_SQL
    rows = [dict(row) for row in (await session.execute(statement, params)).mappings()]
    if not rows:
        matches, calls, period = await _empty_counts(session, params, mode)
        return SearchPage(mode=mode, total_matches=matches, total_calls=calls, period_calls=period)

    head = rows[0]
    items = await _with_paths(session, [_item(row) for row in rows])
    if mode != "browse":
        items = await _decorate(session, items, params["query"])
    return SearchPage(
        mode=mode,
        total_matches=int(head["total_matches"]),
        total_calls=int(head["total_calls"]),
        period_calls=int(head["period_calls"]),
        items=tuple(items),
    )


async def _empty_counts(
    session: AsyncSession, params: dict[str, Any], mode: SearchMode
) -> tuple[int, int, int]:
    """Счётчики, когда страница пуста.

    Нужны именно тогда: интерфейс отличает «ничего не найдено за период с
    сотней звонков» от «в периоде вообще нет звонков» (состояния 02 и 01
    прототипа), а без строк первый запрос счётчиков не вернул.
    """
    period = await session.scalar(
        text(f"SELECT count(*) FROM speech_calls AS c WHERE {_FILTERS_SQL}"), params
    )
    total_matches = 0
    total_calls = 0
    if mode != "browse":
        found = (
            (
                await session.execute(
                    text(
                        f"""
                    SELECT coalesce(sum(match_count), 0) AS matches, count(*) AS calls
                      FROM (
                        SELECT s.call_id, count(*) AS match_count
                          FROM speech_transcript_segments AS s
                          JOIN speech_calls AS c ON c.id = s.call_id
                         CROSS JOIN (
                            SELECT websearch_to_tsquery('{FTS_CONFIG}', :query) AS tsq
                         ) AS q
                         WHERE {_FILTERS_SQL} AND s.search_vector @@ q.tsq
                         GROUP BY s.call_id
                      ) AS hits
                    """
                    ),
                    params,
                )
            )
            .mappings()
            .one()
        )
        total_matches, total_calls = int(found["matches"]), int(found["calls"])
    return total_matches, total_calls, int(period or 0)


def _item(row: dict[str, Any]) -> SearchItem:
    """Преобразовать поисковую SQL-строку в DTO звонка."""
    return SearchItem(
        call_id=row["call_id"],
        session_id=str(row["session_id"]),
        started_at=row["started_at"],
        duration_s=int(row["duration_s"] or 0),
        match_count=int(row["match_count"] or 0),
        operator_login=row["operator_login"],
        project_title=row["project_title"],
        sentiment=row["sentiment"],
        audio_status=str(row["audio_status"]),
        processing_status=str(row["processing_status"]),
        segment=row["segment"],
        segment_source=str(row["segment_source"]),
        product=row["product"],
        product_source=str(row["product_source"]),
        source=str(row["source"]),
        ai_node_id=row["ai_node_id"],
    )


async def _with_paths(session: AsyncSession, items: list[SearchItem]) -> list[SearchItem]:
    """Путь ИИ для карточек: дерево читается один раз на страницу, а не на карточку."""
    if not any(item.ai_node_id for item in items):
        return items
    paths = await NodePaths.load(session)
    return [item.model_copy(update={"ai_path": paths.labels(item.ai_node_id)}) for item in items]


async def _decorate(session: AsyncSession, items: list[SearchItem], query: str) -> list[SearchItem]:
    """Дополнить карточки цитатами и номерами реплик с попаданиями."""
    call_ids = [item.call_id for item in items]
    params = {"query": query, "call_ids": call_ids, "quotes": QUOTES_PER_CALL}

    quotes: dict[uuid.UUID, list[Quote]] = {}
    for row in (await session.execute(_QUOTES_SQL, params)).mappings():
        quotes.setdefault(row["call_id"], []).append(
            Quote(
                seq=int(row["seq"]),
                start_ms=int(row["start_ms"]),
                speaker=str(row["speaker"]),
                text=str(row["quote"]),
            )
        )

    seqs: dict[uuid.UUID, list[int]] = {}
    for row in (await session.execute(_HIT_SEQS_SQL, params)).mappings():
        seqs.setdefault(row["call_id"], []).append(int(row["seq"]))

    return [
        SearchItem(
            **{
                **item.__dict__,
                "quotes": tuple(quotes.get(item.call_id, ())),
                "hit_seqs": tuple(seqs.get(item.call_id, ())),
            }
        )
        for item in items
    ]
