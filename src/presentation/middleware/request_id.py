from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.application.context import (
    _request_id as _request_id,
    current_request_id as current_request_id,
)

REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _normalize_request_id(value: str | None) -> str:
    """Принять корректный идентификатор запроса либо создать новый."""
    if value and _REQUEST_ID_RE.fullmatch(value):
        return value
    return str(uuid.uuid4())


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Передать нормализованный идентификатор запроса в ответ и контекст."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Обработать HTTP-запрос и передать управление следующему обработчику."""
        request_id = _normalize_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = _request_id.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
