"""Доставка личности и отображение отказа доступа в HTTP."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from src.application.authorization import (
    PERM_ADMIN as PERM_ADMIN,
    PERM_DOWNLOAD_AUDIO as PERM_DOWNLOAD_AUDIO,
    PERM_EXPORT as PERM_EXPORT,
    PERM_QUALITY as PERM_QUALITY,
    PERM_SEARCH as PERM_SEARCH,
    Principal as Principal,
)
from src.application.dto.authentication import SESSION_COOKIE_NAME, Credentials
from src.presentation.http.deps import CasesDep


async def current_principal(request: Request, cases: CasesDep) -> Principal:
    """Передать cookie и заголовок сценарию проверки личности и вернуть результат."""
    return await cases.authentication.current(
        Credentials(
            session_token=request.cookies.get(SESSION_COOKIE_NAME, ""),
            authorization=request.headers.get("authorization"),
        )
    )


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require(permission: str) -> Any:
    """Объявить HTTP-зависимость права; прикладной сценарий также проверяет доступ."""

    async def _guard(principal: CurrentPrincipal) -> Principal:
        """По подтверждённой личности пропустить запрос или вернуть 403."""
        if not principal.can(permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        return principal

    _guard.speech_permission = permission  # type: ignore[attr-defined]
    return Depends(_guard)
