"""Планирование обслуживания: правила и выбор звонков принадлежат бэкенду."""

from datetime import UTC, datetime

from src.application.dto.calls import CallSource
from src.application.dto.daily_ingest import DayShare
from src.application.dto.maintenance import (
    CatchUpCommand,
    MaintenancePlan,
    MaintenanceRequest,
    QualityQueueReport,
    QualityQueueRequest,
)
from src.application.dto.quality_batch import CandidateSelection
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.evaluation import EvaluationRepository
from src.application.ports.maintenance import QualityQueueRepository
from src.application.quality.batch import validate_selection
from src.application.worker_access import authorize_worker
from src.domain.calls.period import period_bounds
from src.domain.calls.schedule import CollectionSchedule


class WorkerMaintenanceService:
    """Планировать приём и оценку; не скачивать аудио и не обращаться к LLM."""

    def __init__(
        self,
        schedule: CollectionSchedule,
        repository: QualityQueueRepository,
        evaluations: EvaluationRepository,
        *,
        worker_token: str,
    ) -> None:
        """Принять политику расписания, порты хранения и служебный ключ."""
        self._schedule = schedule
        self._repository = repository
        self._evaluations = evaluations
        self._worker_token = worker_token

    def plan_authorized(
        self, command: MaintenanceRequest, authorization: str | None, *, now: datetime | None = None
    ) -> MaintenancePlan:
        """Выдать следующий срок и параметры действия после проверки служебного доступа."""
        authorize_worker(self._worker_token, authorization)
        ingest = CatchUpCommand(
            source=CallSource(command.action),
            timezone=self._schedule.timezone,
            priority=20 if command.action == "telphin" else None,
            day_share=DayShare(
                share=self._schedule.immediate_share,
                night_from=self._schedule.night_from,
                night_until=self._schedule.night_until,
                timezone=self._schedule.timezone,
            )
            if command.action == "ncc"
            else None,
        )
        instant = now or datetime.now(UTC)
        run_at = self._schedule.next_run(command.action, instant, command.last_started)
        return MaintenancePlan(
            action=command.action,
            run_at=run_at,
            wait_seconds=max(0, (run_at - instant).total_seconds()),
            ingest=ingest,
        )

    async def enqueue(self, command: QualityQueueRequest) -> QualityQueueReport:
        """Локально выбрать готовый Телфин и поставить независимые задания без служебного HTTP."""
        query = command.selection
        try:
            validate_selection(query)
            if query.limit is not None and query.limit > 1000:
                raise ValueError("quality batch limit is 1000")
            if query.drop_stale:
                return QualityQueueReport(
                    stale=await self._evaluations.drop_stale(apply=query.apply), applied=query.apply
                )
            call_id = None
            if query.call is not None:
                call_id = str((await self._evaluations.find_call(query.call)).call_id)
            start = end = None
            if query.first is not None and query.last is not None:
                if query.last < query.first:
                    raise ValueError("--to is earlier than --from")
                start, end = period_bounds(query.first, query.last, self._schedule.timezone)
        except LookupError as exc:
            raise ApplicationError(ErrorKind.NOT_FOUND, "quality input is unavailable") from exc
        except ValueError as exc:
            raise ApplicationError(ErrorKind.INVALID_INPUT, str(exc)) from exc
        return await self._repository.schedule(
            CandidateSelection(
                date_from=start,
                date_to=end,
                force=query.force or query.call is not None,
                skip_failed=query.skip_failed,
                limit=query.limit or 1000,
            ),
            call_id=call_id,
            apply=query.apply,
        )
