"""Независимый сценарий LLM-анализа по загруженному DTO без очереди и файлов аудио."""

import logging
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from src.application.analysis.interpretation import (
    ANALYSIS_RULES_VERSION,
    AnalysisInterpretationService,
)
from src.application.dto.analysis import (
    AnalysisInterpretationContext,
    AnalysisInterpretationRequest,
)
from src.application.dto.analysis_computation import AnalysisComputation
from src.application.dto.analysis_storage import (
    AnalysisReferencePayload,
    ReferenceCallReason,
    ReferenceClassifierNode,
)
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
    ) -> AnalysisComputation:
        """Вызвать LLM по согласованному входу и вернуть проверенное вычисление.

        Args:
            call_id: UUID разбираемого звонка.
            data: Согласованный вход с расшифровкой и справочниками.
            backend: Порт модели; None сохраняет прежний класс ошибки настройки.

        Returns:
            Проверенный work, факты пригодного вызова и снимок справочников.

        Raises:
            StageError: При пустом входе, отказе модели или непригодных ответах.

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
        reference_state: Literal["sent", "empty", "omitted_too_large"] = (
            "sent" if reasons else "empty"
        )
        if len(reference) > REASONS_PROMPT_LIMIT:
            logger.getChild("work").warning(
                "the reason reference is %d characters, the workflow accepts %d: sent without it",
                len(reference),
                REASONS_PROMPT_LIMIT,
            )
            reasons, reference = (), ""
            reference_state = "omitted_too_large"
        references = AnalysisReferencePayload(
            scenario="analysis",
            classifier=tuple(
                ReferenceClassifierNode.model_validate(node.model_dump())
                for node in data.classifier
            ),
            reasons=tuple(
                ReferenceCallReason.model_validate(reason.model_dump()) for reason in reasons
            ),
            reason_reference_state=reference_state,
        )
        inputs: dict[str, JsonValue] = {
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
        return await self._run_until_valid(backend, inputs, call_id, context, references)

    async def _run_until_valid(
        self,
        backend: AnalysisBackend,
        inputs: dict[str, JsonValue],
        call_id: UUID,
        context: AnalysisInterpretationContext,
        reference: AnalysisReferencePayload,
    ) -> AnalysisComputation:
        """Вызвать модель до пригодного результата либо вернуть прежний класс отказа.

        Принимает порты, вход модели, задание и DTO контекста. Неправильный
        ответ повторяется до MAX_OUTPUT_ATTEMPTS; сетевые ошибки сразу
        передаются очереди через StageError. Правила ответа применяет сервис.
        """
        last: InvalidAnalysisOutput | None = None
        for attempt in range(1, MAX_OUTPUT_ATTEMPTS + 1):
            try:
                response = await backend.run(inputs)
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
                work = self._interpreter.interpret(
                    AnalysisInterpretationRequest(outputs=response.outputs, context=context)
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
                continue
            return AnalysisComputation(
                work=work,
                execution=response.execution,
                reference=reference,
                rules_version=ANALYSIS_RULES_VERSION,
                model_call_index=attempt,
            )

        raise StageError(
            JobErrorClass.INVALID_OUTPUT,
            f"analysis workflow returned an unusable answer {MAX_OUTPUT_ATTEMPTS} times: {last}",
        )
