"""Новая ревизия готова только при наличии обеих добавленных таблиц."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from src.infrastructure.persistence.session import speech_schema_is_ready
from tests.speech_db_support import speech_test_database_url

DATABASE_URL = speech_test_database_url()


@pytest.fixture
async def connection() -> AsyncIterator[AsyncConnection]:
    """Открыть откатываемую транзакцию отдельной тестовой БД, не меняя её итоговую схему."""
    if not DATABASE_URL:
        pytest.skip("SPEECH_POSTGRES_TEST_URL is required for schema readiness proofs")
    engine = create_async_engine(DATABASE_URL, hide_parameters=True)
    try:
        async with engine.connect() as value, value.begin():
            try:
                yield value
            finally:
                await value.rollback()
    finally:
        await engine.dispose()


async def test_new_migration_is_ready(connection) -> None:
    """После head API принимает настоящую схему, а не только умеет выдавать отказ."""
    async with AsyncSession(bind=connection) as session:
        assert await speech_schema_is_ready(session)


@pytest.mark.parametrize("revision", ["sp_0016_evaluation_jobs", "sp_unknown_future"])
async def test_unsupported_revision_is_not_ready(connection, revision) -> None:
    """Предыдущая и неизвестная будущая ревизии не обходят точную проверку."""
    await connection.execute(
        text("UPDATE alembic_version SET version_num = :revision"), {"revision": revision}
    )
    async with AsyncSession(bind=connection) as session:
        assert not await speech_schema_is_ready(session)


@pytest.mark.parametrize("table", ["speech_call_analyses", "speech_analysis_references"])
async def test_head_without_one_of_the_new_tables_is_not_ready(connection, table) -> None:
    """Правильная отметка Alembic без фактической таблицы не объявляет API готовым."""
    await connection.execute(text(f'ALTER TABLE "{table}" RENAME TO "{table}_absent_test"'))
    async with AsyncSession(bind=connection) as session:
        assert not await speech_schema_is_ready(session)
