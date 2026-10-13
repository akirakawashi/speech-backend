"""Служебное расписание получения записей; LLM выполняется локально в бэкенде."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from src.application.dto.maintenance import (
    MaintenancePlan,
    MaintenanceRequest,
)
from src.application.quality.scheduling import WorkerMaintenanceService

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


async def maintenance_dep(request: Request) -> WorkerMaintenanceService:
    """Получить сценарий расписания из DI; отсутствие привязки — ошибка сборки."""
    raise RuntimeError("worker maintenance dependency is not installed")


@router.post("/worker-plan", response_model=MaintenancePlan)
async def plan_worker_action(
    command: MaintenanceRequest,
    service: Annotated[WorkerMaintenanceService, Depends(maintenance_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> MaintenancePlan:
    """Передать действие и момент предыдущего запуска; вернуть следующую команду."""
    return service.plan_authorized(command, authorization)
