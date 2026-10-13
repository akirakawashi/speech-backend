"""Этап анализа: загрузить снимок, вызвать модель и передать ответ сервису разбора.

Отказ анализа оставляет расшифрованный звонок доступным для поиска.
Результат сохраняется через порт в транзакции вызывающего; долгие операции
и самостоятельный разбор ответа выполняются до этой транзакции.
"""

from __future__ import annotations

import logging
from typing import Any

from src.application.analysis.interpretation import AnalysisInterpretationService
from src.application.dto.analysis import (
    AnalysisInterpretationContext,
    AnalysisInterpretationRequest,
)
from src.application.dto.pipeline import AnalyzeWork
from src.application.dto.pipeline_input import AnalysisInput
from src.application.errors.analysis import InvalidAnalysisOutput
from src.application.errors.external import AnalysisError, AnalysisNotConfigured
from src.application.pipeline.stages import StageDeps, StageError, StageHandler, StageResult
from src.application.ports.pipeline import StagePersistence
from src.application.ports.queue import ClaimedJob
from src.domain.calls.classifier import ClassifierIndex
from src.domain.calls.contracts import JobErrorClass, JobKind, ProcessingStatus, RoleMethod
from src.domain.calls.reasons import REASONS_PROMPT_LIMIT, prompt_reasons

logger = logging.getLogger(__name__)

#: Повтор чтения ответа внутри попытки задания: неправильная схема не должна
#: сразу завершать звонок терминальным invalid_output. Сохранён предел три.
MAX_OUTPUT_ATTEMPTS = 3


class AnalyzeHandler(StageHandler[AnalyzeWork]):
    """Получить разбор звонка через модель и самостоятельный сервис интерпретации."""

    kind = JobKind.ANALYZE

    def __init__(self, interpreter: AnalysisInterpretationService | None = None) -> None:
        """Принять сервис разбора; при прямом вызове создать его без внешних ресурсов."""
        self._interpreter = (
            interpreter if interpreter is not None else AnalysisInterpretationService()
        )

    async def work(self, job: ClaimedJob, deps: StageDeps) -> AnalyzeWork:
        """Загрузить вход, вызвать модель и вернуть проверенный DTO для атомарной записи."""
        data = await self._load(job, deps)
        if not data.segments:
            raise StageError(
                JobErrorClass.NOT_FOUND, f"call {job.call_id} has no segments to analyse"
            )
        if deps.analysis is None:
            raise StageError(JobErrorClass.PROVIDER_ERROR, "the worker has no analysis client")
        classifier = ClassifierIndex(node.model_dump() for node in data.classifier)
        if not len(classifier):
            raise StageError(
                JobErrorClass.INVALID_OUTPUT,
                "the classifier is empty: load it with "
                "python -m src.presentation.cli.seed_classifier --apply",
            )

        needs_roles = data.call.role_method == RoleMethod.NONE.value
        reasons = data.reasons
        reference = prompt_reasons(reasons)
        if len(reference) > REASONS_PROMPT_LIMIT:
            logger.getChild("work").warning(
                "the reason reference is %d characters, the workflow accepts %d: sent without it",
                len(reference),
                REASONS_PROMPT_LIMIT,
            )
            reasons, reference = (), ""
        inputs = {
            "call_id": str(job.call_id),
            "needs_roles": needs_roles,
            "classifier": classifier.prompt_tree(),
            "reasons": reference,
            "segments": [
                {"seq": int(s.seq), "speaker": str(s.speaker), "text": s.text_raw}
                for s in data.segments
            ],
        }
        context = AnalysisInterpretationContext(
            classifier=data.classifier,
            needs_roles=needs_roles,
            reason_codes=frozenset(reason.code for reason in reasons),
        )
        return await self._run_until_valid(deps, inputs, job, context)

    async def _run_until_valid(
        self,
        deps: StageDeps,
        inputs: dict[str, Any],
        job: ClaimedJob,
        context: AnalysisInterpretationContext,
    ) -> AnalyzeWork:
        """Вызвать модель до пригодного результата либо вернуть прежний класс отказа.

        Принимает порты, вход модели, задание и DTO контекста. Неправильный
        ответ повторяется до MAX_OUTPUT_ATTEMPTS; сетевые ошибки сразу
        передаются очереди через StageError. Правила ответа применяет сервис.
        """
        if deps.analysis is None:
            raise RuntimeError("analysis backend is not configured")
        last: InvalidAnalysisOutput | None = None
        for attempt in range(1, MAX_OUTPUT_ATTEMPTS + 1):
            try:
                outputs = await deps.analysis.run(inputs)
            except AnalysisNotConfigured as exc:
                raise StageError(JobErrorClass.INVALID_OUTPUT, str(exc)) from exc
            except AnalysisError as exc:
                if not exc.retryable:
                    raise StageError(
                        JobErrorClass.PROVIDER_ERROR, f"analysis workflow refused: {exc.reason}"
                    ) from exc
                raise StageError(
                    JobErrorClass.TIMEOUT
                    if exc.reason == "timeout"
                    else JobErrorClass.PROVIDER_ERROR,
                    f"analysis workflow failed: {exc.reason}",
                ) from exc
            try:
                return self._interpreter.interpret(
                    AnalysisInterpretationRequest(outputs=outputs, context=context)
                )
            except InvalidAnalysisOutput as exc:
                last = exc
                logger.getChild("work").warning(
                    "call=%s: analysis output does not match the schema (%s/%s): %s",
                    job.call_id,
                    attempt,
                    MAX_OUTPUT_ATTEMPTS,
                    exc,
                )

        raise StageError(
            JobErrorClass.INVALID_OUTPUT,
            f"analysis workflow returned an unusable answer {MAX_OUTPUT_ATTEMPTS} times: {last}",
        )

    async def _load(self, job: ClaimedJob, deps: StageDeps) -> AnalysisInput:
        """Получить DTO снимка через репозиторий; отсутствующий звонок вернуть как NOT_FOUND."""
        if deps.repository is None:
            raise RuntimeError("pipeline repository is not configured")
        data = await deps.repository.load_analysis(job.call_id)
        if data is None:
            raise StageError(JobErrorClass.NOT_FOUND, f"call {job.call_id} is gone")
        return data

    async def store(
        self, persistence: StagePersistence, job: ClaimedJob, work: AnalyzeWork
    ) -> StageResult:
        """Сохранить DTO через порт транзакции и вернуть завершение цепочки."""
        await persistence.save_analysis(job, work)
        return StageResult(processing_status=ProcessingStatus.DONE, next_kind=None)
