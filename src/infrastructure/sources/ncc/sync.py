"""Адаптер вызова синхронизации в сессии HTTP либо собственной транзакции CLI.

Правила согласования находятся в application/catalogues/directories.py,
SQL — в infrastructure/persistence/directories.py. Эти функции сохраняют
контракт существующих технических вызовов и не содержат второй реализации.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.application.catalogues.directories import DirectorySyncService
from src.application.dto.directories import SyncReport as SyncReport
from src.infrastructure.persistence.directories import PostgresDirectoryRepository
from src.infrastructure.sources.ncc.directories import NccDirectorySource
from src.infrastructure.sources.ncc.rest import NccRestClient


async def sync_employees_in(client: NccRestClient, session: AsyncSession) -> SyncReport:
    """В переданной транзакции выполнить синхронизацию сотрудников и вернуть сводку."""
    return await DirectorySyncService(
        NccDirectorySource(client), PostgresDirectoryRepository(session)
    ).employees()


async def sync_projects_in(client: NccRestClient, session: AsyncSession) -> SyncReport:
    """В переданной транзакции обновить известные проекты и вернуть сводку."""
    return await DirectorySyncService(
        NccDirectorySource(client), PostgresDirectoryRepository(session)
    ).projects()


async def sync_employees(
    client: NccRestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> SyncReport:
    """Открыть транзакцию сотрудников, выполнить сценарий и вернуть сводку после commit."""
    async with sessionmaker() as session, session.begin():
        return await sync_employees_in(client, session)


async def sync_projects(
    client: NccRestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> SyncReport:
    """Открыть транзакцию проектов, выполнить сценарий и вернуть сводку после commit."""
    async with sessionmaker() as session, session.begin():
        return await sync_projects_in(client, session)
