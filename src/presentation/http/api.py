"""Служебные HTTP-маршруты живости, готовности, версии и метрик."""

from typing import Annotated

from fastapi import APIRouter, Header, Response

from src.application.dto.system import VersionInfo
from src.presentation.http.deps import SystemDep

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Подтвердить живость процесса без проверки внешних ресурсов."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(system: SystemDep) -> dict[str, str]:
    """Вызвать сценарий готовности; при успешной проверке вернуть ready."""
    await system.ready()
    return {"status": "ready"}


@router.get("/version", response_model=VersionInfo)
async def service_version(system: SystemDep) -> VersionInfo:
    """Вернуть идентификатор сборки и контрактов, полученный сценарием."""
    return system.version()


@router.get("/metrics", include_in_schema=False)
async def metrics(
    system: SystemDep, authorization: Annotated[str | None, Header()] = None
) -> Response:
    """Передать ключ сценарию сбора метрик и сформировать текстовый HTTP-ответ."""
    result = await system.metrics(authorization)
    return Response(content=result.content, media_type="text/plain; version=0.0.4; charset=utf-8")
