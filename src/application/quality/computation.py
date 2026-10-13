"""Общий локальный сценарий ролей, оценки и проверки ответа без операций базы."""

from collections.abc import Mapping, Sequence
from typing import Literal

from src.application.dto.calls import JobErrorClass
from src.application.dto.quality import CURRENT_SCRIPT, TranscriptLine
from src.application.dto.quality_batch import LineSnapshot
from src.application.dto.quality_execution import (
    MAX_QUALITY_COMPLETIONS,
    QualityTotals,
    QualityWork,
)
from src.application.errors.quality import QualityModelError
from src.application.pipeline.stages import StageError
from src.application.quality.evaluation import (
    MAX_OUTPUT_ATTEMPTS,
    QualityError,
    QualityModel,
    evaluate,
)
from src.application.quality.roles import RoleAttributionError, prepare_roles, without_roles


class _LimitedQualityModel:
    """Сохранить предел модельных запросов и для прямого выполнения сценария."""

    def __init__(self, client: QualityModel) -> None:
        """Принять модель без настроек/сессий; счётчик живёт в рамках одного вычисления."""
        self._client = client
        self._count = 0

    @property
    def model(self) -> str:
        """Вернуть имя исходной модели для версионируемого результата."""
        return self._client.model

    async def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        """Вызвать порт с готовыми сообщениями; превышение бюджета даёт QualityError."""
        if self._count >= MAX_QUALITY_COMPLETIONS:
            raise QualityError("quality request limit exceeded")
        self._count += 1
        return await self._client.complete(messages)


async def compute_quality(
    snapshot: LineSnapshot, client: QualityModel, *, retry_model_errors: bool = False
) -> QualityWork:
    """Вычислить итог по неизменному снимку, сохраняя правила ролей/оценки/сверки.

    База не изменяется. Повторяемый сетевой отказ при retry_model_errors
    передаётся очереди как StageError; иначе возвращается нормализованный
    QualityWork с отказом и уже определёнными ролями. Пределы формата прежние.
    """
    model = _LimitedQualityModel(client)
    prepared: Sequence[TranscriptLine] = snapshot.lines
    evaluation = None
    failure: Literal["invalid_evaluation", "roles_undetermined", "model_error"] | None = None
    try:
        if without_roles(prepared):
            prepared = await prepare_roles(model, prepared, max_attempts=MAX_OUTPUT_ATTEMPTS)
        evaluation = await evaluate(model, prepared)
    except RoleAttributionError:
        failure = "roles_undetermined"
    except QualityModelError as exc:
        if exc.retryable and retry_model_errors:
            error = (
                JobErrorClass.TIMEOUT if exc.reason == "timeout" else JobErrorClass.PROVIDER_ERROR
            )
            raise StageError(error, "quality model request failed") from exc
        failure = "model_error"
    except QualityError as exc:
        failure = "roles_undetermined" if exc.code == "roles_undetermined" else "invalid_evaluation"
    roles = {
        after.seq: after.speaker
        for before, after in zip(snapshot.lines, prepared, strict=True)
        if before.speaker != after.speaker
    }
    if evaluation is not None:
        roles.update(evaluation.speaker_roles)
        evaluation = evaluation.model_copy(update={"speaker_roles": tuple(roles.items())})
    return QualityWork(
        snapshot=snapshot,
        model_name=client.model,
        script_version=CURRENT_SCRIPT.version,
        evaluation=evaluation,
        totals=QualityTotals(
            score=evaluation.score,
            passed_count=evaluation.passed_count,
            stage_count=evaluation.stage_count,
            all_passed=evaluation.all_passed,
        )
        if evaluation is not None
        else None,
        failure_reason=failure,
        roles=tuple(roles.items()),
    )
