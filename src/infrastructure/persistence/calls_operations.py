"""SQL и внешние адаптеры операций calls."""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.authorization import (
    PERM_QUALITY,
)
from src.application.dto.api import (
    CallResponse,
    ClassifierCount,
    ClassifierOption,
    ClassifierResponse,
    MeResponse,
    OptionModel,
    OptionsResponse,
    QuoteModel,
    SearchItemModel,
    SearchResponse,
    SegmentModel,
    StatsResponse,
)
from src.application.dto.calls import (
    SPEECH_TERMINAL_PROCESSING_STATUSES,
    CallSource,
)
from src.application.dto.calls_operations import (
    CallCardQuery,
    ClassifierQuery,
    MeQuery,
    OperatorsQuery,
    ProjectsQuery,
    SearchQuery,
    StatsQuery,
)
from src.application.errors.requests import ApplicationError, ErrorKind
from src.infrastructure.persistence.classifier import (
    NODES_SQL,
    TOPIC_LEVEL,
    NodePaths,
    level_match,
    suggestion_labels,
)
from src.infrastructure.persistence.quality_scores import call_scores
from src.infrastructure.persistence.search import (
    FTS_CONFIG,
    HIGHLIGHT_START,
    HIGHLIGHT_STOP,
    SearchFilters,
    search_calls,
)
from src.infrastructure.persistence.users import remember_user
from src.setting.config import Settings

DEFAULT_PERIOD_DAYS = 30

TOP_TOPICS = 10

PERIOD_ALL: Final[str] = "all"

_STATS_SQL = text(
    """
    SELECT count(*) AS processed,
           count(t.id) AS transcribed,
           count(*) FILTER (
               WHERE c.processing_status <> ALL(CAST(:terminal AS text[]))
           ) AS queued
      FROM speech_calls AS c
      LEFT JOIN speech_transcripts AS t ON t.call_id = c.id
     WHERE c.source = :source
       AND (CAST(:date_from AS timestamptz) IS NULL OR c.started_at >= :date_from)
       AND (CAST(:date_to   AS timestamptz) IS NULL OR c.started_at <  :date_to)
    """
)

_AI_NODE_CALLS_SQL = text(
    """
    SELECT c.ai_node_id AS node_id, count(*) AS calls
      FROM speech_calls AS c
     WHERE c.ai_node_id IS NOT NULL
       AND c.source = :source
       AND (CAST(:date_from AS timestamptz) IS NULL OR c.started_at >= :date_from)
       AND (CAST(:date_to   AS timestamptz) IS NULL OR c.started_at <  :date_to)
     GROUP BY c.ai_node_id
    """
)

_CALL_SQL = text(
    """
    SELECT c.id, c.session_id, c.started_at, c.ended_at, c.duration_s, c.direction,
           c.sentiment, c.summary, c.audio_status, c.processing_status, c.analysis_status,
           c.segment, c.segment_source, c.product, c.product_source, c.source,
           c.ai_node_id, c.operator_node_id, c.ai_suggestion, c.ai_suggestion_node_id,
           c.call_reason, c.call_reason_code, r.label AS call_reason_label,
           o.login AS operator_login, p.title AS project_title,
           tr.role_method
      FROM speech_calls AS c
      LEFT JOIN speech_operators AS o ON o.id = c.operator_id
      LEFT JOIN speech_ncc_projects AS p ON p.id = c.project_id
      LEFT JOIN speech_transcripts AS tr ON tr.call_id = c.id
      LEFT JOIN speech_call_reasons AS r ON r.code = c.call_reason_code
     WHERE c.id = :call_id
    """
)

_SEGMENTS_SQL = text(
    """
    SELECT seq, speaker, start_ms, end_ms, text_raw
      FROM speech_transcript_segments
     WHERE call_id = :call_id
     ORDER BY seq
    """
)

_SEGMENTS_HIGHLIGHTED_SQL = text(
    f"""
    WITH q AS (SELECT websearch_to_tsquery('{FTS_CONFIG}', :query) AS tsq)
    SELECT s.seq, s.speaker, s.start_ms, s.end_ms,
           CASE WHEN s.search_vector @@ q.tsq
                THEN ts_headline('{FTS_CONFIG}', s.text_raw, q.tsq,
                                 'HighlightAll=TRUE,StartSel={HIGHLIGHT_START},'
                                 'StopSel={HIGHLIGHT_STOP}')
                ELSE s.text_raw
           END AS text_raw
      FROM speech_transcript_segments AS s
     CROSS JOIN q
     WHERE s.call_id = :call_id
     ORDER BY s.seq
    """
)

_CALL_HITS_SQL = text(
    """
    SELECT seq
      FROM speech_transcript_segments
     CROSS JOIN (SELECT websearch_to_tsquery('russian', :query) AS tsq) AS q
     WHERE call_id = :call_id AND search_vector @@ q.tsq
     ORDER BY seq
    """
)


class PostgresCallsRepository:
    """Реализовать операции calls в сессии и ресурсах вызывающего."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """Получить общую сессию запроса и явную конфигурацию адаптеров."""
        self._session = session
        self._settings = settings

    async def me(self, command: MeQuery) -> MeResponse:
        """Кто я и что мне позволено.

        Заодно единственное место, где обновляется `speech_users`: администратору
        нужно видеть, кто заходил и с какими правами, не открывая Keycloak.
        """
        session = self._session
        principal = command.actor
        await remember_user(session, principal)
        await session.commit()
        return MeResponse(
            sub=principal.sub,
            username=principal.username,
            display_name=principal.display_name,
            roles=list(principal.roles),
            permissions=sorted(principal.permissions),
            auth_mode=principal.auth_mode,
        )

    async def search(self, command: SearchQuery) -> SearchResponse:
        """Выдача (SRC-01…05). `node_id` — ветка классификатора по классификации ИИ.

        `source` — чьи звонки (S-35), по умолчанию `ncc`; почему так — в описании
        модуля. Доля от периода считается в том же источнике.
        """
        session = self._session
        principal = command.actor
        q = command.q
        date_from = command.date_from
        date_to = command.date_to
        operator_id = command.operator_id
        project_id = command.project_id
        node_id = command.node_id
        segment = command.segment
        product = command.product
        source = command.source
        page = command.page
        size = command.size
        start, end = date_from, date_to
        result = await search_calls(
            session,
            query=q,
            filters=SearchFilters(
                date_from=start,
                date_to=end,
                operator_id=operator_id,
                project_id=project_id,
                node_id=node_id,
                segment=segment.value if segment else None,
                product=product.value if product else None,
                source=source.value,
            ),
            limit=size,
            offset=(page - 1) * size,
        )
        scores = (
            await call_scores(session, [item.call_id for item in result.items], source=source.value)
            if principal.can(PERM_QUALITY)
            else {}
        )
        return SearchResponse(
            mode=result.mode,
            total_matches=result.total_matches,
            total_calls=result.total_calls,
            period_calls=result.period_calls,
            share=result.share_of_period,
            items=[
                SearchItemModel(
                    call_id=item.call_id,
                    session_id=item.session_id,
                    started_at=item.started_at,
                    duration_s=item.duration_s,
                    match_count=item.match_count,
                    operator_login=item.operator_login,
                    project_title=item.project_title,
                    ai_node_id=item.ai_node_id,
                    ai_path=list(item.ai_path),
                    sentiment=item.sentiment,
                    audio_status=item.audio_status,
                    processing_status=item.processing_status,
                    segment=item.segment,
                    segment_source=item.segment_source,
                    product=item.product,
                    product_source=item.product_source,
                    source=CallSource(item.source),
                    quality_score=scores.get(item.call_id),
                    quotes=[
                        QuoteModel(
                            seq=quote.seq,
                            start_ms=quote.start_ms,
                            speaker=quote.speaker,
                            text=quote.text,
                        )
                        for quote in item.quotes
                    ],
                    hit_seqs=list(item.hit_seqs),
                )
                for item in result.items
            ],
        )

    async def stats(self, command: StatsQuery) -> StatsResponse:
        """KPI периода и десять главных тем (SRC-07, SRC-08).

        «Расшифровано» — звонки, у которых есть расшифровка: с этого момента текст
        находится поиском и открывается в карточке.

        `source` — тот же, что у выдачи (S-35): KPI над списком Телфина, посчитанные
        по NCC, описывали бы не тот список.
        """
        session = self._session
        date_from = command.date_from
        date_to = command.date_to
        source = command.source
        start, end = date_from, date_to
        params = {
            "source": source.value,
            "date_from": start,
            "date_to": end,
            "terminal": [status.value for status in SPEECH_TERMINAL_PROCESSING_STATUSES],
        }
        row = (await session.execute(_STATS_SQL, params)).mappings().one()
        counts = {
            found["node_id"]: int(found["calls"])
            for found in (await session.execute(_AI_NODE_CALLS_SQL, params)).mappings()
        }
        paths = await NodePaths.load(session) if counts else NodePaths(())
        branches = paths.rollup(counts)
        topics = sorted(
            (
                (node, calls)
                for node, calls in branches.items()
                if int(paths.node(node)["level"]) == TOPIC_LEVEL
            ),
            key=lambda item: (-item[1], paths.order(item[0])),
        )[:TOP_TOPICS]
        return StatsResponse(
            processed=int(row["processed"]),
            transcribed=int(row["transcribed"]),
            queued=int(row["queued"]),
            topics=[
                ClassifierCount(node_id=node, path=list(paths.labels(node)), calls=calls)
                for node, calls in topics
            ],
        )

    async def call_card(self, command: CallCardQuery) -> CallResponse:
        """Карточка звонка с репликами.

        `q` не фильтрует реплики, а подсвечивает: пользователь пришёл сюда из
        выдачи и должен видеть весь разговор, а не только совпавшие места.

        Параметра `source` здесь нет намеренно (S-35): звонок открывается по своему
        идентификатору, чей бы он ни был, а источник приходит полем ответа.
        """
        session = self._session
        call_id = command.call_id
        q = command.q
        row = (await session.execute(_CALL_SQL, {"call_id": call_id})).mappings().first()
        if row is None:
            raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail="not_found")
        query = q.strip()
        segments = (
            await session.execute(
                _SEGMENTS_HIGHLIGHTED_SQL if query else _SEGMENTS_SQL,
                {"call_id": call_id, "query": query} if query else {"call_id": call_id},
            )
        ).mappings()
        hits: list[int] = []
        if q.strip():
            hits = [
                int(hit["seq"])
                for hit in (
                    await session.execute(_CALL_HITS_SQL, {"call_id": call_id, "query": q.strip()})
                ).mappings()
            ]
        paths = (
            await NodePaths.load(session)
            if row["ai_node_id"] or row["operator_node_id"]
            else NodePaths(())
        )
        ai_path = paths.labels(row["ai_node_id"])
        operator_path = paths.labels(row["operator_node_id"])
        suggestion = suggestion_labels(row["ai_suggestion"])
        return CallResponse(
            call_id=row["id"],
            session_id=str(row["session_id"]),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            duration_s=int(row["duration_s"] or 0),
            direction=str(row["direction"]),
            operator_login=row["operator_login"],
            project_title=row["project_title"],
            ai_node_id=row["ai_node_id"],
            ai_path=list(ai_path),
            operator_node_id=row["operator_node_id"],
            operator_path=list(operator_path),
            level_match=list(level_match(operator_path, ai_path)),
            sentiment=row["sentiment"],
            summary=row["summary"],
            ai_suggestion=list(suggestion),
            ai_suggestion_is_new=bool(suggestion) and row["ai_suggestion_node_id"] is None,
            call_reason=row["call_reason"],
            call_reason_code=row["call_reason_code"],
            call_reason_label=row["call_reason_label"],
            audio_status=str(row["audio_status"]),
            processing_status=str(row["processing_status"]),
            analysis_status=str(row["analysis_status"]),
            segment=row["segment"],
            segment_source=str(row["segment_source"]),
            product=row["product"],
            product_source=str(row["product_source"]),
            source=CallSource(row["source"]),
            role_method=row["role_method"],
            segments=[
                SegmentModel(
                    seq=int(s["seq"]),
                    speaker=str(s["speaker"]),
                    start_ms=int(s["start_ms"]),
                    end_ms=int(s["end_ms"]),
                    text=str(s["text_raw"]),
                )
                for s in segments
            ],
            hit_seqs=hits,
        )

    async def operators(self, command: OperatorsQuery) -> OptionsResponse:
        """Значения фильтра «оператор» — только те, у кого есть звонки.

        Иначе список растёт бывшими сотрудниками, и выбрать из него можно то, что
        ничего не найдёт. По той же причине — только звонки источника `source`
        (S-35): оператор Телфина во вкладке NCC — тоже выбор пустоты.
        """
        session = self._session
        source = command.source
        rows = (
            await session.execute(
                text(
                    "SELECT o.id, coalesce(o.full_name, o.login) AS label   FROM "
                    "speech_operators AS o  WHERE EXISTS (SELECT 1 FROM speech_calls "
                    "AS c                 WHERE c.operator_id = o.id AND c.source = "
                    ":source)  ORDER BY label"
                ),
                {"source": source.value},
            )
        ).mappings()
        return OptionsResponse(
            items=[OptionModel(id=row["id"], label=str(row["label"])) for row in rows]
        )

    async def projects(self, command: ProjectsQuery) -> OptionsResponse:
        """Значения фильтра «очередь» — проекты со звонками источника `source` (S-35).

        Проект Телфина лежит в той же таблице проектов, что и очереди NCC, и без
        условия на источник попадал бы в список вкладки NCC. Во вкладке Телфина
        экран зовёт этот фильтр «проект»: у исходящих звонков очереди нет.
        """
        session = self._session
        source = command.source
        rows = (
            await session.execute(
                text(
                    "SELECT p.id, p.title AS label FROM speech_ncc_projects AS p  "
                    "WHERE EXISTS (SELECT 1 FROM speech_calls AS c                 "
                    "WHERE c.project_id = p.id AND c.source = :source)  ORDER BY "
                    "p.title"
                ),
                {"source": source.value},
            )
        ).mappings()
        return OptionsResponse(
            items=[OptionModel(id=row["id"], label=str(row["label"])) for row in rows]
        )

    async def classifier(self, command: ClassifierQuery) -> ClassifierResponse:
        """Дерево классификатора для фильтра «Классификация» (SP-310) — плоско.

        С выключенными узлами: у них есть звонки, и ссылка с таким узлом должна
        показать, чем отфильтрована выдача. Предлагать ли их для нового выбора,
        решает экран.
        """
        session = self._session
        rows = (await session.execute(NODES_SQL)).mappings()
        return ClassifierResponse(
            items=[
                ClassifierOption(
                    id=row["id"],
                    parent_id=row["parent_id"],
                    level=int(row["level"]),
                    label=str(row["label"]),
                    sort_order=int(row["sort_order"]),
                    is_active=bool(row["is_active"]),
                )
                for row in rows
            ]
        )
