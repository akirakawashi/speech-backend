"""SQL и внешние адаптеры операций quality_read."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dto.api import (
    CallQualityResponse,
    QualityBuckets,
    QualityCallItem,
    QualityCallsResponse,
    QualityCallStage,
    QualityObjectionModel,
    QualityObjectionShare,
    QualityOperatorScore,
    QualityOperatorStage,
    QualityOutcomeShare,
    QualityStageModel,
    QualityStageShare,
    QualitySummaryResponse,
    QualityWeek,
)
from src.application.dto.calls import ProcessingStatus
from src.application.dto.files import XLSX_MEDIA_TYPE, FileResult
from src.application.dto.quality import CURRENT_SCRIPT
from src.application.dto.quality_read_operations import (
    CallQualityQuery,
    QualityCallsQuery,
    QualityReviewFileQuery,
    QualitySummaryQuery,
)
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.quality.reporting import (
    BUCKET_BOUNDS as _BUCKET_BOUNDS,
    SCORE_FAIR,
    SCORE_GOOD,
    bucket_of,
    mandatory_keys as _mandatory_keys,
    stage_label as _stage_label,
    stage_mandatory as _stage_mandatory,
)
from src.infrastructure.persistence.http_audit import record
from src.infrastructure.reports import quality as quality_review
from src.setting.config import Settings

_CALL_SOURCE_SQL = text("SELECT source FROM speech_calls WHERE id = :call_id")

_EVALUATION_SQL = text(
    """
    SELECT e.call_id, e.script_version, e.model_name, e.evaluated_at, e.script_applies,
           e.score, e.passed_count, e.stage_count, e.all_passed, e.outcome, e.stages,
           e.objections, e.summary
      FROM speech_call_evaluations AS e
      JOIN speech_transcripts AS t ON t.call_id = e.call_id
     WHERE e.call_id = :call_id
       AND e.script_version = :script_version
       AND e.evaluated_at >= t.created_at
       AND e.failure_reason IS NULL
    """
)

_FAILURE_SQL = text(
    """
    SELECT e.failure_reason
      FROM speech_call_evaluations AS e
      JOIN speech_transcripts AS t ON t.call_id = e.call_id
     WHERE e.call_id = :call_id AND e.script_version = :script_version
       AND e.failure_reason IS NOT NULL AND e.evaluated_at >= t.created_at
    """
)

_FROM: Final[str] = """
      FROM speech_call_evaluations AS e
      JOIN speech_calls AS c ON c.id = e.call_id
      JOIN speech_transcripts AS t ON t.call_id = e.call_id
"""

_WHERE: Final[str] = """
     WHERE e.script_version = :script_version
       AND c.source = :source
       AND e.evaluated_at >= t.created_at
       AND e.failure_reason IS NULL
       AND (CAST(:date_from AS timestamptz) IS NULL OR c.started_at >= :date_from)
       AND (CAST(:date_to   AS timestamptz) IS NULL OR c.started_at <  :date_to)
       AND (CAST(:operator_id AS uuid) IS NULL OR c.operator_id = :operator_id)
       AND (NOT CAST(:unassigned AS boolean) OR c.operator_id IS NULL)
"""

_COVERAGE_FROM: Final[str] = """
      FROM speech_calls AS c
      LEFT JOIN speech_transcripts AS t ON t.call_id = c.id
      LEFT JOIN speech_call_evaluations AS e
        ON e.call_id = c.id AND e.script_version = :script_version
       AND e.evaluated_at >= t.created_at
"""

_COVERAGE_WHERE: Final[str] = """
     WHERE c.source = :source
       AND (CAST(:date_from AS timestamptz) IS NULL OR c.started_at >= :date_from)
       AND (CAST(:date_to AS timestamptz) IS NULL OR c.started_at < :date_to)
       AND (CAST(:operator_id AS uuid) IS NULL OR c.operator_id = :operator_id)
       AND (NOT CAST(:unassigned AS boolean) OR c.operator_id IS NULL)
"""

_NO_TRANSCRIPT_STATUSES: Final[list[str]] = [
    ProcessingStatus.FAILED.value,
    ProcessingStatus.SKIPPED_SHORT.value,
    ProcessingStatus.SKIPPED_NO_AUDIO.value,
]

EvaluationStatus = Literal["evaluated", "failed", "pending", "unavailable"]

_STATUS_SQL: Final[str] = """CASE
    WHEN e.call_id IS NOT NULL AND e.failure_reason IS NOT NULL THEN 'failed'
    WHEN e.call_id IS NOT NULL THEN 'evaluated'
    WHEN t.id IS NULL
         AND c.processing_status = ANY(CAST(:closed AS text[])) THEN 'unavailable'
    ELSE 'pending' END"""

_COVERAGE_SQL = text(
    f"""
    SELECT count(*) AS total,
           count(*) FILTER (WHERE {_STATUS_SQL} = 'failed') AS failed,
           count(*) FILTER (WHERE {_STATUS_SQL} = 'pending') AS pending,
           count(*) FILTER (WHERE {_STATUS_SQL} = 'unavailable') AS unavailable
    {_COVERAGE_FROM}
    {_COVERAGE_WHERE}
    """
)


Bucket = Literal["good", "fair", "poor"]


_VIOLATION: Final[str] = """EXISTS (
               SELECT 1 FROM jsonb_array_elements(e.stages) AS m(value)
                WHERE m.value->>'key' = ANY(CAST(:mandatory AS text[]))
                  AND (m.value->>'applicable')::boolean
                  AND NOT (m.value->>'passed')::boolean
           )"""

_BUCKET_COUNTS: Final[str] = f"""
           count(*) FILTER (WHERE e.script_applies AND e.score >= {SCORE_GOOD}) AS good,
           count(*) FILTER (
               WHERE e.script_applies AND e.score >= {SCORE_FAIR} AND e.score < {SCORE_GOOD}
           ) AS fair,
           count(*) FILTER (WHERE e.script_applies AND e.score < {SCORE_FAIR}) AS poor,
           count(*) FILTER (WHERE e.script_applies AND {_VIOLATION}) AS violations
"""

_TOTALS_SQL = text(
    f"""
    SELECT count(*) AS evaluated,
           count(*) FILTER (WHERE e.script_applies) AS calls,
           count(*) FILTER (WHERE e.all_passed) AS fully_correct,
           avg(e.score) FILTER (WHERE e.script_applies) AS average_score,
           {_BUCKET_COUNTS}
    {_FROM}
    {_WHERE}
    """
)

_STAGES_SQL = text(
    f"""
    SELECT s.value->>'key' AS key,
           count(*) AS calls,
           count(*) FILTER (WHERE (s.value->>'applicable')::boolean) AS applicable,
           count(*) FILTER (WHERE (s.value->>'passed')::boolean) AS passed,
           count(*) FILTER (
               WHERE (s.value->>'applicable')::boolean AND NOT (s.value->>'passed')::boolean
           ) AS failed
    {_FROM}
     CROSS JOIN LATERAL jsonb_array_elements(e.stages) AS s(value)
    {_WHERE}
       AND e.script_applies
     GROUP BY 1
    """
)

_OBJECTIONS_SQL = text(
    f"""
    SELECT o.value->>'type' AS type,
           count(*) AS raised,
           count(*) FILTER (WHERE (o.value->>'handled')::boolean) AS handled
    {_FROM}
     CROSS JOIN LATERAL jsonb_array_elements(e.objections) AS o(value)
    {_WHERE}
       AND e.script_applies
     GROUP BY 1
    """
)

_OUTCOMES_SQL = text(
    f"""
    SELECT e.outcome AS key, count(*) AS calls
    {_FROM}
    {_WHERE}
       AND e.script_applies
     GROUP BY 1
    """
)

_OPERATORS_SQL = text(
    f"""
    SELECT c.operator_id, op.login, op.full_name,
           count(*) AS calls,
           avg(e.score) AS average_score,
           count(*) FILTER (WHERE e.all_passed) AS fully_correct,
           {_BUCKET_COUNTS}
    {_FROM}
      LEFT JOIN speech_operators AS op ON op.id = c.operator_id
    {_WHERE}
       AND e.script_applies
     GROUP BY c.operator_id, op.login, op.full_name
     ORDER BY average_score DESC, calls DESC
    """
)

_OPERATOR_STAGES_SQL = text(
    f"""
    SELECT c.operator_id, s.value->>'key' AS key,
           count(*) FILTER (WHERE (s.value->>'applicable')::boolean) AS applicable,
           count(*) FILTER (
               WHERE (s.value->>'applicable')::boolean AND NOT (s.value->>'passed')::boolean
           ) AS failed
    {_FROM}
     CROSS JOIN LATERAL jsonb_array_elements(e.stages) AS s(value)
    {_WHERE}
       AND e.script_applies
     GROUP BY 1, 2
    """
)

_WEEKS_SQL = text(
    f"""
    SELECT CAST(date_trunc('week', c.started_at AT TIME ZONE :tz) AS date) AS week_start,
           count(*) AS calls,
           count(*) FILTER (WHERE e.all_passed) AS fully_correct,
           avg(e.score) AS average_score,
           {_BUCKET_COUNTS}
    {_FROM}
    {_WHERE}
       AND e.script_applies
     GROUP BY 1
     ORDER BY 1
    """
)

_CALL_FILTERS: Final[str] = f"""
       AND (CAST(:applies AS boolean) IS NULL OR e.script_applies = CAST(:applies AS boolean))
       AND (CAST(:evaluation_status AS text) IS NULL
            OR {_STATUS_SQL} = CAST(:evaluation_status AS text))
       AND (CAST(:score_min AS integer) IS NULL OR e.score >= CAST(:score_min AS integer))
       AND (CAST(:score_max AS integer) IS NULL OR e.score <= CAST(:score_max AS integer))
       AND (CAST(:outcome AS text) IS NULL OR e.outcome = CAST(:outcome AS text))
       AND (CAST(:stage AS text) IS NULL OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(e.stages) AS f(value)
                WHERE f.value->>'key' = CAST(:stage AS text)
                  AND (f.value->>'applicable')::boolean = CAST(:stage_applicable AS boolean)
                  AND (CAST(:stage_passed AS boolean) IS NULL
                       OR (f.value->>'passed')::boolean = CAST(:stage_passed AS boolean))
           ))
       AND (NOT CAST(:violations AS boolean) OR {_VIOLATION})
"""

StageState = Literal["failed", "done", "skipped"]

_STAGE_STATES: Final[dict[str, tuple[bool, bool | None]]] = {
    "failed": (True, False),
    "done": (True, True),
    "skipped": (False, None),
}

CallSort = Literal["newest", "worst", "best"]

_CALL_ORDER: Final[dict[str, str]] = {
    "newest": "c.started_at DESC, c.id DESC",
    "worst": "e.score ASC NULLS LAST, c.started_at DESC, c.id DESC",
    "best": "e.score DESC NULLS LAST, c.started_at DESC, c.id DESC",
}

_CALLS_SQL: Final = {
    sort: text(
        f"""
    SELECT c.id AS call_id, c.session_id, c.started_at, c.duration_s, c.operator_id,
           op.login AS operator_login, op.full_name AS operator_name,
           e.script_applies, e.score, e.passed_count, e.stage_count, e.all_passed,
           e.outcome, e.summary, e.stages, e.failure_reason,
           {_STATUS_SQL} AS evaluation_status
    {_COVERAGE_FROM}
      LEFT JOIN speech_operators AS op ON op.id = c.operator_id
    {_COVERAGE_WHERE}
    {_CALL_FILTERS}
     ORDER BY {order}
     LIMIT :limit OFFSET :offset
    """
    )
    for sort, order in _CALL_ORDER.items()
}

_CALLS_COUNT_SQL = text(
    f"""
    SELECT count(*) AS total
    {_COVERAGE_FROM}
    {_COVERAGE_WHERE}
    {_CALL_FILTERS}
    """
)

_REVIEW_SQL = text(
    f"""
    WITH picked AS (
        SELECT e.id
        {_FROM}
        {_WHERE}
           AND e.script_applies
         ORDER BY random()
         LIMIT :limit
    )
    SELECT c.id AS call_id, c.started_at, c.duration_s,
           op.login AS operator_login, op.full_name AS operator_name, e.stages
      FROM picked
      JOIN speech_call_evaluations AS e ON e.id = picked.id
      JOIN speech_calls AS c ON c.id = e.call_id
      LEFT JOIN speech_operators AS op ON op.id = c.operator_id
    """
)

CALLS_PAGE_SIZE: Final[int] = 50

CALLS_PAGE_MAX: Final[int] = 200


def _share(part: int, whole: int) -> float:
    """Вычислить долю; при пустом охвате вернуть ноль."""
    return round(part / whole, 4) if whole else 0.0


def _buckets(row: Any) -> QualityBuckets:
    """Преобразовать счётчики SQL-строки в DTO разрядов качества."""
    return QualityBuckets(
        good=int(row["good"] or 0), fair=int(row["fair"] or 0), poor=int(row["poor"] or 0)
    )


def _ints(value: Any) -> list[int]:
    """Оставить в значении ответа только допустимые целые номера реплик."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int) and (not isinstance(item, bool))]


def _review_filename(start: datetime | None, end: datetime | None, timezone: str) -> str:
    """Имя файла с сутками периода, как их видит колл-центр. Без границы — без дат."""
    if start is None or end is None:
        return "quality-review.xlsx"
    zone = ZoneInfo(timezone)
    first = start.astimezone(zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    return f"quality-review-{first.isoformat()}_{last.isoformat()}.xlsx"


class PostgresQualityReadRepository:
    """Реализовать операции quality_read в сессии и ресурсах вызывающего."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """Получить общую сессию запроса и явную конфигурацию адаптеров."""
        self._session = session
        self._settings = settings

    async def call_quality(self, command: CallQualityQuery) -> CallQualityResponse:
        """Оценка звонка по текущей редакции скрипта.

        `404 not_evaluated` — оценки нет. `404 not_applicable` — звонок другого
        источника: по скрипту он не оценивается и оценён не будет, поэтому карточка
        не должна обещать оценку.
        """
        session = self._session
        call_id = command.call_id
        script = CURRENT_SCRIPT
        source = (
            await session.execute(_CALL_SOURCE_SQL, {"call_id": call_id})
        ).scalar_one_or_none()
        if source is not None and source != script.source.value:
            raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail="not_applicable")
        row = (
            (
                await session.execute(
                    _EVALUATION_SQL, {"call_id": call_id, "script_version": script.version}
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            reason = (
                await session.execute(
                    _FAILURE_SQL, {"call_id": call_id, "script_version": script.version}
                )
            ).scalar_one_or_none()
            if reason:
                raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail=f"quality_{reason}")
            raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail="not_evaluated")
        return CallQualityResponse(
            call_id=row["call_id"],
            script_version=str(row["script_version"]),
            model_name=str(row["model_name"]),
            evaluated_at=row["evaluated_at"],
            script_applies=bool(row["script_applies"]),
            score=row["score"],
            passed_count=row["passed_count"],
            stage_count=row["stage_count"],
            all_passed=row["all_passed"],
            outcome=row["outcome"],
            outcome_label=script.outcome_label(row["outcome"]),
            summary=row["summary"],
            stages=[
                QualityStageModel(
                    key=str(item.get("key")),
                    label=_stage_label(script, str(item.get("key"))),
                    mandatory=_stage_mandatory(script, str(item.get("key"))),
                    applicable=bool(item.get("applicable")),
                    passed=bool(item.get("passed")),
                    evidence=_ints(item.get("evidence")),
                    comment=str(item.get("comment") or ""),
                )
                for item in row["stages"] or []
                if isinstance(item, dict)
            ],
            objections=[
                QualityObjectionModel(
                    type=str(item.get("type")),
                    label=script.objection_label(str(item.get("type"))),
                    seq=item.get("seq") if isinstance(item.get("seq"), int) else None,
                    handled=bool(item.get("handled")),
                    evidence=_ints(item.get("evidence")),
                    comment=str(item.get("comment") or ""),
                )
                for item in row["objections"] or []
                if isinstance(item, dict)
            ],
        )

    async def quality_summary(self, command: QualitySummaryQuery) -> QualitySummaryResponse:
        """Сводка за период: разряды по баллу, этапы, возражения, итоги, сотрудники, недели.

        Считаются звонки того источника, для которого написан скрипт; остальные в
        сводку не входят ни оценёнными, ни ожидающими.

        Период — как у поиска: без границ — последние тридцать дней, `period=all` —
        всё время. Этапы, возражения и итоги перечислены в порядке скрипта, и в
        списке есть каждый — с нулём, если в периоде его не было: экран не должен
        менять раскладку от того, какие пункты встретились.
        """
        session = self._session
        settings = self._settings
        date_from = command.date_from
        date_to = command.date_to
        operator_id = command.operator_id
        unassigned = command.unassigned
        script = CURRENT_SCRIPT
        start, end = date_from, date_to
        params: dict[str, Any] = {
            "script_version": script.version,
            "source": script.source.value,
            "date_from": start,
            "date_to": end,
            "operator_id": operator_id,
            "unassigned": unassigned,
            "mandatory": _mandatory_keys(script),
            "closed": _NO_TRANSCRIPT_STATUSES,
        }
        totals = (await session.execute(_TOTALS_SQL, params)).mappings().one()
        coverage = (await session.execute(_COVERAGE_SQL, params)).mappings().one()
        calls = int(totals["calls"] or 0)
        fully_correct = int(totals["fully_correct"] or 0)
        stage_rows = {
            str(row["key"]): row for row in (await session.execute(_STAGES_SQL, params)).mappings()
        }
        objection_rows = {
            str(row["type"]): row
            for row in (await session.execute(_OBJECTIONS_SQL, params)).mappings()
        }
        outcome_rows = {
            str(row["key"]): int(row["calls"])
            for row in (await session.execute(_OUTCOMES_SQL, params)).mappings()
        }
        operator_rows = (await session.execute(_OPERATORS_SQL, params)).mappings().all()
        operator_stages: dict[uuid.UUID | None, dict[str, Any]] = {}
        for row in (await session.execute(_OPERATOR_STAGES_SQL, params)).mappings():
            operator_stages.setdefault(row["operator_id"], {})[str(row["key"])] = row
        week_rows = (
            (await session.execute(_WEEKS_SQL, {**params, "tz": settings.speech_display_timezone}))
            .mappings()
            .all()
        )
        stages = []
        for stage in script.stages:
            found = stage_rows.get(stage.key)
            passed = int(found["passed"]) if found else 0
            stages.append(
                QualityStageShare(
                    key=stage.key,
                    label=stage.label,
                    mandatory=stage.mandatory,
                    calls=int(found["calls"]) if found else 0,
                    applicable=int(found["applicable"]) if found else 0,
                    passed=passed,
                    failed=int(found["failed"]) if found else 0,
                    share=_share(passed, calls),
                )
            )
        objections = []
        for objection in script.objections:
            seen = objection_rows.get(objection.key)
            raised = int(seen["raised"]) if seen else 0
            handled = int(seen["handled"]) if seen else 0
            objections.append(
                QualityObjectionShare(
                    type=objection.key,
                    label=objection.label,
                    raised=raised,
                    handled=handled,
                    share=_share(handled, raised),
                )
            )
        return QualitySummaryResponse(
            script_version=script.version,
            total=int(coverage["total"]),
            failed=int(coverage["failed"]),
            pending=int(coverage["pending"]),
            unavailable=int(coverage["unavailable"]),
            evaluated=int(totals["evaluated"] or 0),
            calls=calls,
            fully_correct=fully_correct,
            fully_correct_share=_share(fully_correct, calls),
            average_score=round(float(totals["average_score"]), 1)
            if totals["average_score"] is not None
            else None,
            buckets=_buckets(totals),
            violations=int(totals["violations"] or 0),
            stages=stages,
            objections=objections,
            outcomes=[
                QualityOutcomeShare(
                    key=key,
                    label=label,
                    calls=outcome_rows.get(key, 0),
                    share=_share(outcome_rows.get(key, 0), calls),
                )
                for key, label in script.outcomes
            ],
            operators=[
                QualityOperatorScore(
                    operator_id=row["operator_id"],
                    login=row["login"],
                    full_name=row["full_name"],
                    calls=int(row["calls"]),
                    average_score=round(float(row["average_score"] or 0), 1),
                    fully_correct=int(row["fully_correct"]),
                    fully_correct_share=_share(int(row["fully_correct"]), int(row["calls"])),
                    buckets=_buckets(row),
                    violations=int(row["violations"] or 0),
                    stages=[
                        QualityOperatorStage(
                            key=stage.key,
                            applicable=int(found["applicable"]) if found else 0,
                            failed=int(found["failed"]) if found else 0,
                        )
                        for stage in script.stages
                        for found in (operator_stages.get(row["operator_id"], {}).get(stage.key),)
                    ],
                )
                for row in operator_rows
            ],
            weeks=[
                QualityWeek(
                    week_start=row["week_start"],
                    calls=int(row["calls"]),
                    fully_correct=int(row["fully_correct"]),
                    fully_correct_share=_share(int(row["fully_correct"]), int(row["calls"])),
                    average_score=round(float(row["average_score"]), 1)
                    if row["average_score"] is not None
                    else None,
                    buckets=_buckets(row),
                )
                for row in week_rows
            ],
        )

    async def quality_calls(self, command: QualityCallsQuery) -> QualityCallsResponse:
        """Записи периода из источника скрипта с фильтрами и порядком.

        По умолчанию видны также ошибки оценки, ожидающие её звонки и звонки, у
        которых расшифровки уже не будет (`unavailable`).
        `evaluation_status` отбирает состояние оценки; `applies=false` — звонки
        вне скрипта с причиной в `summary`. Баллы и этапы относятся только к
        актуальным успешным оценкам.

        `bucket` — разряд по баллу, `outcome` — итог звонка, `violations` — не
        выполнено обязательное условие. `stage` — этап, а `stage_state` — каким он
        был в звонке: не выполнен (так по умолчанию — ищут обычно ошибки), выполнен
        или не понадобился. По второму находят звонки, на которые стоит равняться.
        Неизвестный этап или итог — `422`, а не пустой список: опечатка в фильтре
        не должна выглядеть как «таких звонков нет».
        """
        session = self._session
        date_from = command.date_from
        date_to = command.date_to
        operator_id = command.operator_id
        unassigned = command.unassigned
        applies = command.applies
        evaluation_status = command.evaluation_status
        bucket = command.bucket
        stage = command.stage
        stage_state = command.stage_state
        outcome = command.outcome
        violations = command.violations
        sort = command.sort
        limit = command.limit
        offset = command.offset
        script = CURRENT_SCRIPT
        start, end = date_from, date_to
        score_min, score_max = _BUCKET_BOUNDS[bucket] if bucket else (None, None)
        mandatory = _mandatory_keys(script)
        params: dict[str, Any] = {
            "script_version": script.version,
            "source": script.source.value,
            "date_from": start,
            "date_to": end,
            "operator_id": operator_id,
            "unassigned": unassigned,
            "applies": applies,
            "evaluation_status": evaluation_status,
            "closed": _NO_TRANSCRIPT_STATUSES,
            "score_min": score_min,
            "score_max": score_max,
            "stage": stage,
            "stage_applicable": _STAGE_STATES[stage_state][0],
            "stage_passed": _STAGE_STATES[stage_state][1],
            "outcome": outcome,
            "violations": violations,
            "mandatory": mandatory,
        }
        total = int((await session.execute(_CALLS_COUNT_SQL, params)).scalar_one())
        rows = (
            (await session.execute(_CALLS_SQL[sort], {**params, "limit": limit, "offset": offset}))
            .mappings()
            .all()
        )
        items = []
        for row in rows:
            verdicts = {
                str(item.get("key")): item for item in row["stages"] or [] if isinstance(item, dict)
            }
            stages = [
                QualityCallStage(
                    key=known.key,
                    applicable=bool(verdicts[known.key].get("applicable")),
                    passed=bool(verdicts[known.key].get("passed")),
                )
                for known in script.stages
                if known.key in verdicts
            ]
            failed = [item for item in stages if item.applicable and (not item.passed)]
            score = int(row["score"]) if row["score"] is not None else None
            items.append(
                QualityCallItem(
                    call_id=row["call_id"],
                    session_id=str(row["session_id"]),
                    started_at=row["started_at"],
                    duration_s=int(row["duration_s"] or 0),
                    operator_id=row["operator_id"],
                    operator_login=row["operator_login"],
                    operator_name=row["operator_name"],
                    evaluation_status=row["evaluation_status"],
                    failure_reason=row["failure_reason"],
                    script_applies=row["script_applies"],
                    score=score,
                    bucket=bucket_of(score),
                    passed_count=row["passed_count"],
                    stage_count=row["stage_count"],
                    all_passed=row["all_passed"],
                    violation=any(item.key in mandatory for item in failed),
                    outcome=row["outcome"],
                    outcome_label=script.outcome_label(row["outcome"]),
                    summary=row["summary"],
                    failed_stages=[_stage_label(script, item.key) for item in failed],
                    stages=stages,
                )
            )
        return QualityCallsResponse(total=total, items=items)

    async def quality_review_file(self, command: QualityReviewFileQuery) -> FileResult:
        """Файл Excel для сверки оценок контролёром: случайные оценённые звонки охвата.

        Охват — тот же, что у сводки: период и сотрудник. Каждый запрос — новая
        случайная выборка. В файле оценка модели по каждому этапу, её пояснение,
        реплики-основания и полный текст разговоров; что куда
        ложится — в `src.infrastructure.reports.quality`.

        `403` — нет права `export`: оценки человек видит, а файл наружу не выносит.
        `404 nothing_to_review` — в охвате нет ни одного звонка, оценённого по
        скрипту: пустой файл выглядел бы как сбой выгрузки.

        Журнал пишется до сборки файла и в той же транзакции, что и выборка, — как
        у отчёта по звонкам: выгрузка, о которой журнал не знает, хуже записи о
        файле, который не собрался.
        """
        session = self._session
        settings = self._settings
        principal = command.actor
        date_from = command.date_from
        date_to = command.date_to
        operator_id = command.operator_id
        unassigned = command.unassigned
        limit = command.limit
        script = CURRENT_SCRIPT
        start, end = date_from, date_to
        rows = (
            (
                await session.execute(
                    _REVIEW_SQL,
                    {
                        "script_version": script.version,
                        "source": script.source.value,
                        "date_from": start,
                        "date_to": end,
                        "operator_id": operator_id,
                        "unassigned": unassigned,
                        "limit": limit,
                    },
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            raise ApplicationError(kind=ErrorKind.NOT_FOUND, detail="nothing_to_review")
        lines = await quality_review.load_lines(session, [row["call_id"] for row in rows])
        calls = [
            quality_review.ReviewCall(
                call_id=row["call_id"],
                started_at=row["started_at"],
                duration_s=int(row["duration_s"] or 0),
                operator=str(row["operator_name"] or row["operator_login"] or ""),
                stages=row["stages"] or [],
                lines=lines.get(row["call_id"], []),
            )
            for row in rows
        ]
        await record(
            session,
            principal,
            "report.quality_review",
            payload={
                "from": start.isoformat() if start else None,
                "to": end.isoformat() if end else None,
                "operator_id": str(operator_id) if operator_id else None,
                "unassigned": unassigned,
                "script_version": script.version,
                "rows": len(calls),
            },
        )
        await session.commit()
        content = await asyncio.to_thread(
            quality_review.build_workbook,
            calls,
            script=script,
            timezone=settings.speech_display_timezone,
            base_url=settings.speech_public_base_url,
        )
        filename = _review_filename(start, end, settings.speech_display_timezone)
        return FileResult(content=content, media_type=XLSX_MEDIA_TYPE, filename=filename)
