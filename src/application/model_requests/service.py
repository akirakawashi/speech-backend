"""Подготовка промпта и выбор модели; внешние HTTP-вызовы здесь не выполняются."""

import json
import re

from pydantic import JsonValue

from src.application.dto.model_execution import (
    ModelMessage,
    ModelPreparationRequest,
    PreparedModelRequest,
)
from src.application.errors.requests import ApplicationError, ErrorKind
from src.application.ports.model_requests import PromptCatalogue
from src.application.worker_access import authorize_worker

_VARIABLE = re.compile(r"\{\{(?P<name>[a-z_]+)\}\}")


def _render_value(value: JsonValue) -> str:
    """Привести вход к тексту промпта: сохранить кириллицу и структуру JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


class ModelRequestService:
    """Собрать готовый запрос из шаблона, входного DTO и политики бэкенда."""

    def __init__(
        self,
        catalogue: PromptCatalogue,
        *,
        model: str,
        free_model: str,
        candidate_model: str,
        max_tokens: int,
        worker_token: str,
    ) -> None:
        """Принять каталог и настройки модели; секрет используется только для доступа."""
        self._catalogue = catalogue
        self._model = model
        self._free_model = free_model
        self._candidate_model = candidate_model
        self._max_tokens = max_tokens
        self._worker_token = worker_token

    async def prepare_authorized(
        self, request: ModelPreparationRequest, authorization: str | None
    ) -> PreparedModelRequest:
        """Проверить служебный Bearer-токен и подготовить запрос для воркера.

        Пустой настроенный токен закрывает маршрут с 503; неверный даёт 401.
        Тело разговора и значение токена в ошибки не включаются.
        """
        authorize_worker(self._worker_token, authorization)
        return await self.prepare(request)

    async def prepare(self, request: ModelPreparationRequest) -> PreparedModelRequest:
        """Проверить именованные входы и вернуть готовый DTO без вызова модели.

        Ошибки входа содержат только имя поля. Неизвестные входы отклоняются,
        необязательные отсутствующие подставляются пустыми.
        """
        template = self._catalogue.get(request.scenario)
        variables = {variable.name: variable for variable in template.variables}
        unknown = set(request.inputs) - set(variables)
        if unknown:
            raise ApplicationError(ErrorKind.INVALID_INPUT, "unknown model input")
        values: dict[str, str] = {}
        for name, variable in variables.items():
            value = request.inputs.get(name)
            if variable.required and (value is None or value == ""):
                raise ApplicationError(ErrorKind.INVALID_INPUT, f"model input is required: {name}")
            rendered = _render_value(value)
            if variable.max_length is not None and len(rendered) > variable.max_length:
                raise ApplicationError(ErrorKind.INVALID_INPUT, f"model input is too long: {name}")
            values[name] = rendered
        production_model = (
            self._free_model
            if request.scenario in ("free_classification", "operator_compare")
            else self._model
        )
        model = self._candidate_model if request.profile == "candidate" else production_model
        if not model:
            raise ApplicationError(ErrorKind.UNAVAILABLE, "analysis model is not configured")
        if request.profile == "candidate" and model == production_model:
            raise ApplicationError(
                ErrorKind.INVALID_INPUT, "candidate and production models must differ"
            )
        messages = tuple(
            ModelMessage(
                role=message.role,
                content=_VARIABLE.sub(lambda match: values[match.group("name")], message.content),
            )
            for message in template.messages
        )
        return PreparedModelRequest(
            contract_version="1.0",
            scenario=request.scenario,
            profile=request.profile,
            prompt_version=template.version,
            model=model,
            messages=messages,
            temperature=0,
            max_tokens=self._max_tokens,
        )
