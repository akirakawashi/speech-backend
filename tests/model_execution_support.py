"""Явные синтетические факты LLM-вызова для подставных портов в тестах."""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue

from src.application.dto.model_execution import (
    ModelExecutionMetadata,
    ModelExecutionResult,
    ModelScenario,
)


def model_result(
    outputs: JsonValue, *, scenario: ModelScenario = "analysis"
) -> ModelExecutionResult:
    """Обернуть ответ подставного порта явными тестовыми метаданными.

    Args:
        outputs: Ответ, включая заведомо непригодный JSON для тестов разбора.
        scenario: Сценарий подставного порта.

    Returns:
        DTO без сети с уникальным UUID и заведомо синтетическими хешами.
    """
    now = datetime.now(UTC)
    return ModelExecutionResult(
        outputs=outputs,
        execution=ModelExecutionMetadata(
            execution_id=uuid4(),
            scenario=scenario,
            profile="production",
            requested_model="test-model",
            prompt_version="test-prompt/1",
            prompt_digest="1" * 64,
            request_fingerprint="2" * 64,
            temperature=0,
            max_tokens=8192,
            started_at=now,
            finished_at=now,
        ),
    )
