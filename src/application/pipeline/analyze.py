"""Сохранённый адаптер этапа analyze; вычисления выполняет самостоятельный сценарий."""

from src.application.analysis.execution import (
    MAX_OUTPUT_ATTEMPTS as MAX_OUTPUT_ATTEMPTS,
    AnalysisExecutionService,
)
from src.application.analysis.interpretation import AnalysisInterpretationService
from src.application.dto.pipeline import AnalyzeWork as AnalyzeWork
from src.application.pipeline.stages import StageDeps, StageError, StageHandler, StageResult
from src.application.ports.pipeline import StagePersistence
from src.application.ports.queue import ClaimedJob
from src.domain.calls.contracts import JobErrorClass, JobKind, ProcessingStatus


class AnalyzeHandler(StageHandler[AnalyzeWork]):
    """Сохранить совместимость прежнего конвейера без копии правил LLM-анализа."""

    kind = JobKind.ANALYZE

    def __init__(self, interpreter: AnalysisInterpretationService | None = None) -> None:
        """Внедрить сервис интерпретации в независимый вычислительный сценарий."""
        self._execution = AnalysisExecutionService(interpreter)

    async def work(self, job: ClaimedJob, deps: StageDeps) -> AnalyzeWork:
        """Прочитать вход через порт и вернуть проверенный результат без записи."""
        if deps.repository is None:
            raise RuntimeError("pipeline repository is not configured")
        data = await deps.repository.load_analysis(job.call_id)
        if data is None:
            raise StageError(JobErrorClass.NOT_FOUND, f"call {job.call_id} is gone")
        return await self._execution.execute(job.call_id, data, deps.analysis)

    async def store(
        self, persistence: StagePersistence, job: ClaimedJob, work: AnalyzeWork
    ) -> StageResult:
        """Записать DTO через прежний порт транзакции и завершить цепочку."""
        await persistence.save_analysis(job, work)
        return StageResult(processing_status=ProcessingStatus.DONE, next_kind=None)
