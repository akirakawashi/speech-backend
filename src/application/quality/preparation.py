"""Бизнес-сценарий оценки в бэкенде; сетевые запросы выполняет воркер."""

from collections.abc import Mapping, Sequence

from src.application.dto.model_execution import ModelMessage
from src.application.dto.quality import CURRENT_SCRIPT, TranscriptLine
from src.application.dto.quality_execution import (
    MAX_QUALITY_COMPLETIONS,
    QualityModelRequest,
    QualityStep,
    QualityStepRequest,
    QualityTotals,
    QualityWork,
)
from src.application.errors.quality import QualityModelError
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.evaluation import EvaluationRepository
from src.application.quality.evaluation import MAX_OUTPUT_ATTEMPTS, QualityError, evaluate
from src.application.quality.roles import RoleAttributionError, prepare_roles, without_roles
from src.application.worker_access import authorize_worker


class _NextCompletion(Exception):
    """Внутренний сигнал: сценарий дошёл до ещё не выполненного запроса модели."""

    def __init__(self, messages: Sequence[Mapping[str, str]]) -> None:
        """Скопировать сообщения, чтобы последующие исправления не меняли выданный DTO."""
        self.messages = tuple(ModelMessage.model_validate(message) for message in messages)


class _RecordedModel:
    """Подставить уже полученные ответы; следующий запрос вернуть вызывающему.

    Объект существует только в рамках одного HTTP-запроса. Он не вызывает LLM
    и позволяет использовать проверенный сценарий ролей, оценки и проверки
    без второй реализации его правил в воркере.
    """

    def __init__(self, model: str, responses: tuple[str, ...], model_error: bool) -> None:
        """Принять имя модели, последовательность ответов и исход последнего запроса."""
        self.model = model
        self._responses = responses
        self._model_error = model_error
        self.consumed = 0

    async def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        """Вернуть записанный ответ, нормализованный отказ либо сигнал следующего шага."""
        if self.consumed < len(self._responses):
            response = self._responses[self.consumed]
            self.consumed += 1
            return response
        if self._model_error:
            raise QualityModelError("unreachable", retryable=True)
        if self.consumed >= MAX_QUALITY_COMPLETIONS:
            raise QualityError("quality request limit exceeded")
        raise _NextCompletion(messages)


class QualityPreparationService:
    """Выдать следующий готовый запрос или проверенную оценку одного звонка Телфина."""

    def __init__(
        self, repository: EvaluationRepository, *, model: str, max_tokens: int, worker_token: str
    ) -> None:
        """Принять хранение снимка, параметры модели и ключ служебного доступа."""
        self._repository = repository
        self._model = model
        self._max_tokens = max_tokens
        self._worker_token = worker_token

    async def prepare_authorized(
        self, command: QualityStepRequest, authorization: str | None
    ) -> QualityStep:
        """Проверить доступ и актуальность снимка; не открывать долгую транзакцию."""
        authorize_worker(self._worker_token, authorization)
        if not self._model:
            raise ApplicationError(ErrorKind.UNAVAILABLE, "quality model is not configured")
        try:
            await self._repository.find_call(str(command.call_id))
        except LookupError as exc:
            raise ApplicationError(ErrorKind.NOT_FOUND, "quality input is unavailable") from exc
        current = await self._repository.load_lines(command.call_id)
        snapshot = command.snapshot or current
        if snapshot.lines != current.lines or snapshot.read_at > current.read_at:
            raise ApplicationError(ErrorKind.CONFLICT, "quality input changed")
        model = _RecordedModel(self._model, command.responses, command.model_error)
        prepared: Sequence[TranscriptLine] = snapshot.lines
        evaluation = None
        failure = None
        try:
            if without_roles(prepared):
                prepared = await prepare_roles(model, prepared, max_attempts=MAX_OUTPUT_ATTEMPTS)
            evaluation = await evaluate(model, prepared)
        except _NextCompletion as pending:
            return QualityStep(
                snapshot=snapshot,
                request=QualityModelRequest(
                    model=self._model,
                    messages=pending.messages,
                    max_tokens=self._max_tokens,
                    prompt_version=CURRENT_SCRIPT.version,
                ),
            )
        except RoleAttributionError:
            failure = "roles_undetermined"
        except QualityModelError:
            failure = "model_error"
        except QualityError as exc:
            failure = (
                "roles_undetermined" if exc.code == "roles_undetermined" else "invalid_evaluation"
            )
        if model.consumed != len(command.responses):
            raise ApplicationError(ErrorKind.INVALID_INPUT, "unexpected quality responses")
        roles = {
            after.seq: after.speaker
            for before, after in zip(snapshot.lines, prepared, strict=True)
            if before.speaker != after.speaker
        }
        if evaluation is not None:
            roles.update(evaluation.speaker_roles)
            evaluation = evaluation.model_copy(update={"speaker_roles": tuple(roles.items())})
        return QualityStep(
            snapshot=snapshot,
            result=QualityWork(
                snapshot=snapshot,
                model_name=self._model,
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
            ),
        )
