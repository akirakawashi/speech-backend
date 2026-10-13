"""Переходный контракт шагов оценки; правила разделяются с локальным CPU-сценарием."""

from collections.abc import Mapping, Sequence

from src.application.dto.model_execution import ModelMessage
from src.application.dto.quality import CURRENT_SCRIPT
from src.application.dto.quality_execution import (
    MAX_QUALITY_COMPLETIONS,
    QualityModelRequest,
    QualityStep,
    QualityStepRequest,
)
from src.application.errors.quality import QualityModelError
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.evaluation import EvaluationRepository
from src.application.quality.computation import compute_quality
from src.application.quality.evaluation import QualityError
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
        try:
            result = await compute_quality(snapshot, model)
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
        if model.consumed != len(command.responses):
            raise ApplicationError(ErrorKind.INVALID_INPUT, "unexpected quality responses")
        return QualityStep(snapshot=snapshot, result=result)
