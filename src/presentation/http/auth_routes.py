"""HTTP-маршруты входа, возврата провайдера и выхода."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Annotated, Final

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from src.application.authentication.service import safe_next as safe_next
from src.application.dto import authentication as sessions
from src.application.dto.authentication import LoginAttempt, LoginCallbackQuery
from src.presentation.http.cookies import cookie_secure
from src.presentation.http.deps import CasesDep

router = APIRouter(prefix="/api/v1/auth")
LOGIN_COOKIE_PATH: Final[str] = "/api/v1/auth"
DEFAULT_NEXT: Final[str] = "/"
ERROR_REDIRECT: Final[str] = "/?auth_error="


def _pack(payload: dict[str, str]) -> str:
    """Закодировать временный DTO входа в значение cookie."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unpack(value: str) -> dict[str, str]:
    """Восстановить временный DTO входа из cookie; испорченное значение вернуть пустым."""
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, binascii.Error):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(k): str(v) for k, v in payload.items()}


def _error(code: str) -> RedirectResponse:
    """Сформировать перенаправление после отказа входа."""
    response = RedirectResponse(ERROR_REDIRECT + code, status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(sessions.LOGIN_COOKIE_NAME, path=LOGIN_COOKIE_PATH)
    return response


@router.get("/login")
async def login(
    request: Request, cases: CasesDep, next: Annotated[str | None, Query(max_length=2000)] = None
) -> Response:
    """Передать адрес возврата сценарию входа и установить короткую HttpOnly cookie."""
    result = await cases.authentication.begin_login(next)
    if result.error:
        return _error(result.error)
    response = RedirectResponse(result.destination, status_code=303)
    if result.attempt is not None:
        response.set_cookie(
            key=sessions.LOGIN_COOKIE_NAME,
            value=_pack(result.attempt.model_dump()),
            max_age=sessions.LOGIN_COOKIE_MAX_AGE,
            path=LOGIN_COOKIE_PATH,
            httponly=True,
            samesite="lax",
            secure=cookie_secure(cases.authentication.cookies, request),
        )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    cases: CasesDep,
    code: Annotated[str | None, Query(max_length=4000)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    """Доставить возврат провайдера сценарию, затем установить или убрать cookies."""
    stored = _unpack(request.cookies.get(sessions.LOGIN_COOKIE_NAME, ""))
    try:
        attempt = LoginAttempt.model_validate(stored)
    except ValidationError:
        attempt = None
    result = await cases.authentication.complete_login(
        LoginCallbackQuery(code=code, state=state, error=error, attempt=attempt)
    )
    if result.error:
        return _error(result.error)
    response = RedirectResponse(result.destination, status_code=303)
    if result.token is not None:
        response.set_cookie(
            key=sessions.SESSION_COOKIE_NAME,
            value=result.token,
            max_age=result.ttl_seconds,
            path=sessions.SESSION_COOKIE_PATH,
            httponly=True,
            samesite="lax",
            secure=cookie_secure(cases.authentication.cookies, request),
        )
        response.delete_cookie(sessions.LOGIN_COOKIE_NAME, path=LOGIN_COOKIE_PATH)
    return response


@router.post("/logout")
async def logout(request: Request, cases: CasesDep) -> Response:
    """Погасить сессию сценарием и вернуть адрес внешнего выхода, удалив cookie."""
    result = await cases.authentication.logout(
        request.cookies.get(sessions.SESSION_COOKIE_NAME, "")
    )
    response = JSONResponse(result.model_dump())
    response.delete_cookie(sessions.SESSION_COOKIE_NAME, path=sessions.SESSION_COOKIE_PATH)
    return response
