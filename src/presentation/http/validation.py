"""Безопасные ошибки валидации HTTP: входные реплики и секреты не возвращаются."""

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def internal_validation_error_response(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Скрыть значения служебного ввода; обычные маршруты используют ответ FastAPI."""
    if request.url.path == "/internal/v1/transcription-results":
        return JSONResponse(status_code=422, content={"detail": "invalid transcription result"})
    if request.url.path.startswith("/internal/v1/"):
        return JSONResponse(status_code=422, content={"detail": "invalid internal request"})
    return await request_validation_exception_handler(request, exc)
