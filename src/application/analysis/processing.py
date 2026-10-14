"""Сценарий фонового анализа: согласованный вход, LLM вне транзакции, защищённый commit."""

import logging
from collections.abc import Sequence

from src.application.analysis.execution import AnalysisExecutionService
from src.application.dto.analysis_processing import AnalysisClaim, AnalysisOutcome, AnalysisSnapshot
from src.application.dto.calls import JobErrorClass, JobKind, JobLane, JobStatus, is_terminal_error
from src.application.dto.pipeline import AnalyzeWork
from src.application.errors.analysis_processing import AnalysisProcessingBusy
from src.application.errors.stages import StageError
from src.application.ports.analysis import AnalysisBackend
from src.application.ports.analysis_processing import AnalysisProcessingRepository
from src.application.ports.queue import ExpiredJob
from src.domain.calls.analysis_processing import LostAnalysisLease, require_analysis_lease

logger = logging.getLogger(__name__)


class AnalysisProcessingService:
    """Обработать analyze в бэкенде; не обращаться к API этого же проекта или к аудио."""

    def __init__(
        self,
        repository: AnalysisProcessingRepository,
        model: AnalysisBackend,
        execution: AnalysisExecutionService,
    ) -> None:
        """Внедрить хранение, прямой LLM-порт и существующие правила разбора."""
        self._repository = repository
        self._model = model
        self._execution = execution

    async def process(self, claim: AnalysisClaim) -> AnalysisOutcome:
        """Вызвать LLM и подтвердить исход только после защищённого commit.

        Вход — nonce захвата. Отказ модели сохраняет прежний бюджет повторов;
        смена снимка откладывает задание. Устаревший захват не меняет данные.
        Ошибка базы и внешняя отмена выходят вызывающему: незавершённая аренда
        восстанавливается очередью после истечения срока.
        """
        try:
            snapshot = await self._repository.load(claim)
        except LostAnalysisLease:
            return AnalysisOutcome(status="stale")
        work: AnalyzeWork | None = None
        error_class: JobErrorClass | None = None
        try:
            if snapshot is None:
                raise StageError(JobErrorClass.NOT_FOUND, "call has no transcript")
            work = await self._execution.execute(claim.call_id, snapshot.data, self._model)
        except StageError as exc:
            error_class = exc.error_class
        except Exception as exc:
            logger.error(
                "analysis execution failed: job=%s call=%s type=%s",
                claim.job_id,
                claim.call_id,
                type(exc).__name__,
            )
            error_class = JobErrorClass.UNEXPECTED_ERROR
        try:
            return await self._finish(claim, snapshot, work, error_class)
        except LostAnalysisLease:
            return AnalysisOutcome(status="stale")
        except AnalysisProcessingBusy:
            deferred = await self._repository.defer(claim)
            return AnalysisOutcome(status="deferred" if deferred else "stale")

    async def _finish(
        self,
        claim: AnalysisClaim,
        snapshot: AnalysisSnapshot | None,
        work: AnalyzeWork | None,
        error_class: JobErrorClass | None,
    ) -> AnalysisOutcome:
        """Сверить снимок и lease, атомарно записать успех либо отказ; не выполнять сеть."""
        async with self._repository.transaction() as transaction:
            lease = await transaction.lock(claim.job_id, claim.call_id)
            require_analysis_lease(claim, lease, now=await transaction.database_time())
            if lease is not None and lease.other_processing:
                raise AnalysisProcessingBusy("analysis_pipeline_is_busy")
            current = await transaction.snapshot(claim.call_id)
            if (current.fingerprint if current else None) != (
                snapshot.fingerprint if snapshot else None
            ):
                raise AnalysisProcessingBusy("analysis_input_has_changed")
            if work is not None:
                await transaction.save(claim, work)
                if not await transaction.complete(claim):
                    raise LostAnalysisLease("analysis_claim_is_stale")
                outcome = AnalysisOutcome(status="done")
            else:
                failure = error_class or JobErrorClass.UNEXPECTED_ERROR
                status = await transaction.fail(
                    claim, failure, terminal=is_terminal_error(JobKind.ANALYZE, failure)
                )
                if status is None:
                    raise LostAnalysisLease("analysis_claim_is_stale")
                if status is JobStatus.DEAD:
                    await transaction.mark_failed(claim.call_id)
                outcome = AnalysisOutcome(
                    status="dead" if status is JobStatus.DEAD else "retry", error_class=failure
                )
        return outcome

    async def mark_expired(self, expired: ExpiredJob) -> None:
        """Отметить окончательный отказ только для всё ещё dead задания analyze.

        Повторная постановка или новый активный этап запрещают позднюю проекцию
        отказа. Состояние звонка и проверка задания находятся в одной транзакции.
        """
        if expired.kind is not JobKind.ANALYZE or expired.status is not JobStatus.DEAD:
            return
        async with self._repository.transaction() as transaction:
            lease = await transaction.lock(expired.id, expired.call_id)
            if (
                lease is not None
                and lease.kind is JobKind.ANALYZE
                and lease.status is JobStatus.DEAD
                and not lease.other_processing
            ):
                await transaction.mark_failed(expired.call_id)

    async def repair_expired_failures(self, lanes: Sequence[JobLane]) -> None:
        """Повторить проекцию dead после обрыва между возвратом очереди и записью звонка."""
        for expired in await self._repository.unprojected_failures(lanes):
            try:
                await self.mark_expired(expired)
            except AnalysisProcessingBusy:
                continue
