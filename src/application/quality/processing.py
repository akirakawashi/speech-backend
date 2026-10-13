"""CPU-оценка в бэкенде: локальная модель, актуальный снимок и атомарный исход задания."""

import logging

from src.application.dto.calls import JobErrorClass, JobKind, JobStatus, is_terminal_error
from src.application.dto.evaluation_processing import EvaluationSnapshot
from src.application.dto.job_processing import JobProcessingClaim, JobProcessingOutcome
from src.application.dto.quality import CURRENT_SCRIPT
from src.application.dto.quality_execution import QualityWork
from src.application.errors.job_processing import JobProcessingBusy
from src.application.pipeline.stages import StageError
from src.application.ports.evaluation_processing import EvaluationProcessingRepository
from src.application.quality.computation import compute_quality
from src.application.quality.evaluation import QualityModel
from src.domain.calls.job_processing import LostJobProcessingLease, require_job_processing_lease
from src.domain.quality.processing import (
    EvaluationInputNotReady,
    EvaluationInputUnavailable,
    require_evaluation_input,
)

logger = logging.getLogger(__name__)


class EvaluationProcessingService:
    """Оценить только готовый Телфин; ошибки оценки не меняют доступность звонка."""

    def __init__(self, repository: EvaluationProcessingRepository, model: QualityModel) -> None:
        """Внедрить короткое хранение и прямой HTTP-порт модели без служебного API."""
        self._repository = repository
        self._model = model

    async def process(self, claim: JobProcessingClaim) -> JobProcessingOutcome:
        """Вычислить оценку и подтвердить только commit актуального захвата.

        Смена текста/ролей/транскрипта откладывает попытку. Сетевой отказ
        использует прежний backoff; последняя попытка сохраняет model_error.
        Внешняя отмена и ошибка базы выходят вызывающему для восстановления аренды.
        """
        try:
            snapshot = await self._repository.load(claim)
        except LostJobProcessingLease:
            return JobProcessingOutcome(status="stale")
        work: QualityWork | None = None
        error_class: JobErrorClass | None = None
        try:
            if snapshot is None:
                raise EvaluationInputUnavailable("evaluation_has_no_transcript")
            data = snapshot.data
            require_evaluation_input(
                source=data.call_source,
                status=data.processing_status,
                created_at=data.transcript_created_at,
                read_at=snapshot.read_at,
                other_processing=snapshot.other_processing,
            )
            computed = await compute_quality(
                snapshot.lines,
                self._model,
                retry_model_errors=claim.attempt < snapshot.max_attempts,
            )
            if (
                computed.snapshot != snapshot.lines
                or computed.model_name != self._model.model
                or computed.script_version != CURRENT_SCRIPT.version
            ):
                raise StageError(JobErrorClass.INVALID_OUTPUT, "quality result metadata mismatch")
            work = computed
        except EvaluationInputNotReady:
            return await self._defer(claim)
        except EvaluationInputUnavailable:
            error_class = JobErrorClass.NOT_FOUND
        except StageError as exc:
            error_class = exc.error_class
        except Exception as exc:
            logger.error(
                "quality execution failed: job=%s type=%s", claim.job_id, type(exc).__name__
            )
            error_class = JobErrorClass.UNEXPECTED_ERROR
        try:
            return await self._finish(claim, snapshot, work, error_class)
        except LostJobProcessingLease:
            return JobProcessingOutcome(status="stale")
        except JobProcessingBusy:
            return await self._defer(claim)

    async def _defer(self, claim: JobProcessingClaim) -> JobProcessingOutcome:
        """Отложить только текущий nonce с возвратом попытки; потерянный даёт stale."""
        return JobProcessingOutcome(
            status="deferred" if await self._repository.defer(claim) else "stale"
        )

    async def _finish(
        self,
        claim: JobProcessingClaim,
        snapshot: EvaluationSnapshot | None,
        work: QualityWork | None,
        error_class: JobErrorClass | None,
    ) -> JobProcessingOutcome:
        """Проверить версию и аренду; атомарно записать оценку/отказ с исходом задания."""
        async with self._repository.transaction() as transaction:
            lease = await transaction.lock(claim.job_id, claim.call_id)
            require_job_processing_lease(
                claim, lease, kind=JobKind.EVALUATE, now=await transaction.database_time()
            )
            if lease is not None and lease.other_processing:
                raise JobProcessingBusy("evaluation_pipeline_is_busy")
            current = await transaction.snapshot(claim.call_id)
            if current != (snapshot.data if snapshot else None):
                raise JobProcessingBusy("evaluation_input_has_changed")
            if work is not None:
                if not await transaction.newer_success(
                    claim.call_id, work.script_version, work.snapshot.read_at
                ):
                    await transaction.save(claim, work)
                if not await transaction.complete(claim):
                    raise LostJobProcessingLease("processing_claim_is_stale")
                outcome = JobProcessingOutcome(status="done")
            else:
                failure = error_class or JobErrorClass.UNEXPECTED_ERROR
                status = await transaction.fail(
                    claim, failure, terminal=is_terminal_error(JobKind.EVALUATE, failure)
                )
                if status is None:
                    raise LostJobProcessingLease("processing_claim_is_stale")
                outcome = JobProcessingOutcome(
                    status="dead" if status is JobStatus.DEAD else "retry", error_class=failure
                )
        return outcome
