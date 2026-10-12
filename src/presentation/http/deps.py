"""Абстрактные FastAPI-зависимости сценариев, привязанные сборкой DI."""

from typing import Annotated

from fastapi import Depends, Request

from src.application.http import HttpApplication
from src.application.system.service import SystemService


async def use_cases_dep(request: Request) -> HttpApplication:
    """Запросить сценарии; отсутствие привязки DI означает ошибку сборки."""
    raise RuntimeError("HTTP application dependencies are not installed")


async def system_dep(request: Request) -> SystemService:
    """Запросить служебные сценарии без открытия транзакции пользовательского запроса."""
    raise RuntimeError("System dependencies are not installed")


CasesDep = Annotated[HttpApplication, Depends(use_cases_dep)]
SystemDep = Annotated[SystemService, Depends(system_dep)]
