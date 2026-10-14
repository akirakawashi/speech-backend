"""Независимый сценарий LLM-анализа по загруженному DTO без очереди и файлов аудио."""

import logging
from typing import Any
from uuid import UUID

from src.application.analysis.interpretation import AnalysisInterpretationService
from src.application.dto.analysis import (
    AnalysisInterpretationContext,
    AnalysisInterpretationRequest,
)
from src.application.dto.pipeline import AnalyzeWork
from src.application.dto.pipeline_input import AnalysisInput
from src.application.errors.analysis import InvalidAnalysisOutput
from src.application.errors.external import AnalysisError, AnalysisNotConfigured
from src.application.errors.stages import StageError
from src.application.ports.analysis import AnalysisBackend
from src.domain.calls.classifier import ClassifierIndex
from src.domain.calls.contracts import JobErrorClass, RoleMethod
from src.domain.calls.reasons import REASONS_PROMPT_LIMIT, prompt_reasons

logger = logging.getLogger(__name__)
MAX_OUTPUT_ATTEMPTS = 3


class AnalysisExecutionService:
    """Подготовить вход, вызвать внедрённый LLM-порт и применить сервис интерпретации."""

    def __init__(self, interpreter: AnalysisInterpretationService | None = None) -> None:
        """Принять сервис разбора; при прямом вызове создать его без внешних ресурсов."""
        self._interpreter = (
            interpreter if interpreter is not None else AnalysisInterpretationService()
        )

    async def execute(
        self, call_id: UUID, data: AnalysisInput, backend: AnalysisBackend | None
    ) -> AnalyzeWork:
        """Принять UUID и согласованный вход, вызвать LLM и вернуть AnalyzeWork.

        Транзакции и настройки окружения отсутствуют. Непригодный формат
        повторяется три раза; известные отказы возвращаются как StageError.
        """
        if not data.segments:
            raise StageError(JobErrorClass.NOT_FOUND, f"call {call_id} has no segments to analyse")
        if backend is None:
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
            "call_id": str(call_id),
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
        return await self._run_until_valid(backend, inputs, call_id, context)

    async def _run_until_valid(
        self,
        backend: AnalysisBackend,
        inputs: dict[str, Any],
        call_id: UUID,
        context: AnalysisInterpretationContext,
    ) -> AnalyzeWork:
        """Вызвать модель до пригодного результата либо вернуть прежний класс отказа.

        Принимает порты, вход модели, задание и DTO контекста. Неправильный
        ответ повторяется до MAX_OUTPUT_ATTEMPTS; сетевые ошибки сразу
        передаются очереди через StageError. Правила ответа применяет сервис.
        """
        last: InvalidAnalysisOutput | None = None
        for attempt in range(1, MAX_OUTPUT_ATTEMPTS + 1):
            try:
                outputs = await backend.run(inputs)
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
                    call_id,
                    attempt,
                    MAX_OUTPUT_ATTEMPTS,
                    exc,
                )

        raise StageError(
            JobErrorClass.INVALID_OUTPUT,
            f"analysis workflow returned an unusable answer {MAX_OUTPUT_ATTEMPTS} times: {last}",
        )
