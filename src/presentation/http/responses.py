"""Преобразование файлового результата сценария в HTTP-ответ."""

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from src.application.dto.files import FileResult
from src.application.errors.requests import ApplicationError, ErrorKind

_STATUS_CODES = {
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.INVALID_INPUT: 422,
    ErrorKind.FORBIDDEN: 403,
    ErrorKind.UNAUTHORIZED: 401,
    ErrorKind.CONFLICT: 409,
    ErrorKind.BUSY: 429,
    ErrorKind.UNAVAILABLE: 503,
    ErrorKind.DEPENDENCY: 502,
    ErrorKind.INTERNAL: 500,
}


async def application_error_response(request: Request, exc: ApplicationError) -> JSONResponse:
    """Отобразить ожидаемый отказ сценария в прежний формат ошибки API."""
    return JSONResponse(
        status_code=_STATUS_CODES[exc.kind],
        content={"detail": exc.detail},
        headers={"WWW-Authenticate": "Bearer"} if exc.challenge else None,
    )


def file_response(result: FileResult) -> Response:
    """По байтам и метаданным файла сформировать ответ для скачивания."""
    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={"Content-Disposition": f'attachment; filename="{result.filename}"'},
    )
