"""Планирование обслуживания: правила и выбор звонков принадлежат бэкенду."""

from src.application.dto.maintenance import (
    QualityQueueReport,
    QualityQueueRequest,
)
from src.application.dto.quality_batch import CandidateSelection
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.evaluation import EvaluationRepository
from src.application.ports.maintenance import QualityQueueRepository
from src.application.quality.batch import validate_selection
from src.domain.calls.period import period_bounds
from src.domain.calls.schedule import CollectionSchedule


class WorkerMaintenanceService:
    """Локально выбирать готовые звонки и ставить оценку без HTTP и скачивания."""

    def __init__(
        self,
        schedule: CollectionSchedule,
        repository: QualityQueueRepository,
        evaluations: EvaluationRepository,
    ) -> None:
        """Принять политику расписания и порты хранения."""
        self._schedule = schedule
        self._repository = repository
        self._evaluations = evaluations

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
