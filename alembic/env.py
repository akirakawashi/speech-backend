"""Миграции Speech Analytics — отдельное дерево для отдельной базы.

Почему не ветка в общем дереве, как у registry-ms, rag-ms и остальных. Те
сервисы живут в общей базе `aicistent` и изолированы ролями Postgres, поэтому
ветка с `down_revision`, уходящим в ревизии основного приложения, у них
применяется штатно. У Speech Analytics база **своя**, и та же ветка в ней не
применилась бы: alembic пошёл бы по цепочке вниз и упёрся в ревизии, которых в
этой базе нет и быть не должно.

Второе, менее очевидное: образ миграций по умолчанию выполняет
`alembic upgrade head`. Будь таблицы `speech_*` в общем дереве, эта команда
создала бы их в общей базе — молча и мимо всех ролей. Раздельные деревья
делают такую ошибку невозможной, а не маловероятной.

Отсюда же берётся `target_metadata`: только `SpeechBase.metadata`. Метаданные
основного приложения сюда не попадают, поэтому автогенерация физически не
может предложить создать чужую таблицу.

Запуск:

    alembic -c alembic.ini upgrade head
    alembic -c alembic.ini revision --autogenerate -m "..."
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from src.infrastructure.persistence.base import SpeechBase
from src.setting.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SpeechBase.metadata


def get_url() -> str:
    """Строка подключения к базе Speech Analytics.

    Отката на общий `DATABASE_URL` здесь нет намеренно. У сервисов в общей базе
    такой откат безобиден — база та же. Здесь он означал бы создание таблиц
    `speech_*` в базе основного приложения, то есть ровно ту ошибку, ради
    исключения которой дерево и разделено. Пустая настройка обязана
    остановить миграцию, а не увести её в чужую базу.
    """
    url = (get_settings().speech_ms_database_url or "").strip()
    if not url:
        raise RuntimeError(
            "SPEECH_MS_DATABASE_URL is not set. Speech Analytics migrations run "
            "against their own database; there is deliberately no fallback to "
            "DATABASE_URL, which would create speech_* tables in the main "
            "application's database."
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
