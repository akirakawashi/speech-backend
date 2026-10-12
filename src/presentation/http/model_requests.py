"""Служебный маршрут подготовки запроса: бэкенд задаёт содержание работы."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.application.dto.model_execution import ModelPreparationRequest, PreparedModelRequest
from src.application.model_requests.service import ModelRequestService

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


async def model_validation_error_response(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Скрыть входные значения в отказе служебного маршрута; сохранить ответы остальных."""
    if request.url.path.startswith("/internal/v1/"):
        return JSONResponse(status_code=422, content={"detail": "invalid model request"})
    return await request_validation_exception_handler(request, exc)


async def model_request_dep(request: Request) -> ModelRequestService:
    """Получить сценарий из DI; отсутствие привязки означает ошибку сборки."""
    raise RuntimeError("model preparation dependency is not installed")


@router.post("/model-requests", response_model=PreparedModelRequest)
async def prepare_model_request(
    command: ModelPreparationRequest,
    service: Annotated[ModelRequestService, Depends(model_request_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> PreparedModelRequest:
    """Передать входной DTO сценарию; вернуть готовый запрос или ожидаемый отказ."""
    return await service.prepare_authorized(command, authorization)
