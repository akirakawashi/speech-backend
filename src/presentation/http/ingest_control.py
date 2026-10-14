"""Служебные маршруты решений ingest; роутеры не скачивают записи и не выбирают правила."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from src.application.dto.ingest_control import (
    IngestClaim,
    IngestPlan,
    IngestResolution,
    ResolveIngest,
)
from src.application.ingestion.admission import IngestControlService

router = APIRouter(prefix="/internal/v1/ingest", include_in_schema=False)


async def ingest_control_dep(request: Request) -> IngestControlService:
    """Получить сценарий через DI; отсутствие привязки означает ошибку сборки."""
    raise RuntimeError("ingest control dependency is not installed")


@router.post("/prepare", response_model=IngestPlan)
async def prepare_ingest(
    command: IngestClaim,
    service: Annotated[IngestControlService, Depends(ingest_control_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> IngestPlan:
    """Передать захват и вернуть техническую команду после проверки доступа."""
    return await service.prepare(command, authorization)


@router.post("/resolve", response_model=IngestResolution)
async def resolve_ingest(
    command: ResolveIngest,
    service: Annotated[IngestControlService, Depends(ingest_control_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> IngestResolution:
    """Передать наблюдения источника/файла; вернуть решение бэкенда о продолжении."""
    return await service.resolve(command, authorization)
