"""Служебные роуты: расписание, очередь оценки и подготовка её модельных шагов."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from src.application.dto.maintenance import (
    MaintenancePlan,
    MaintenanceRequest,
    QualityQueueReport,
    QualityQueueRequest,
)
from src.application.dto.quality_execution import QualityStep, QualityStepRequest
from src.application.quality.preparation import QualityPreparationService
from src.application.quality.scheduling import WorkerMaintenanceService

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


async def maintenance_dep(request: Request) -> WorkerMaintenanceService:
    """Получить сценарий расписания из DI; отсутствие привязки — ошибка сборки."""
    raise RuntimeError("worker maintenance dependency is not installed")


async def quality_preparation_dep(request: Request) -> QualityPreparationService:
    """Получить сценарий оценки из DI без импорта его адаптеров хранения."""
    raise RuntimeError("quality preparation dependency is not installed")


@router.post("/worker-plan", response_model=MaintenancePlan)
async def plan_worker_action(
    command: MaintenanceRequest,
    service: Annotated[WorkerMaintenanceService, Depends(maintenance_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> MaintenancePlan:
    """Передать действие и момент предыдущего запуска; вернуть следующую команду."""
    return service.plan_authorized(command, authorization)


@router.post("/quality-jobs", response_model=QualityQueueReport)
async def enqueue_quality(
    command: QualityQueueRequest,
    service: Annotated[WorkerMaintenanceService, Depends(maintenance_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> QualityQueueReport:
    """Передать проверенный охват и вернуть число заданий, без вызова LLM."""
    return await service.enqueue_authorized(command, authorization)


@router.post("/quality-steps", response_model=QualityStep)
async def prepare_quality_step(
    command: QualityStepRequest,
    service: Annotated[QualityPreparationService, Depends(quality_preparation_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> QualityStep:
    """Продолжить оценку по полученным ответам; вернуть запрос модели либо результат."""
    return await service.prepare_authorized(command, authorization)
