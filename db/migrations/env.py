"""Alembic env.py — асинхронный шаблон.

INIT-001 создаёт только рабочий каркас: подключение через тот же движок,
что и приложение (`upravdom.config.get_settings`), без моделей. DB-001
подставит `target_metadata` из `upravdom.models` и добавит первую ревизию.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from upravdom.config import get_settings
from upravdom.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """URL берётся из настроек приложения, а не из alembic.ini (см. файл)."""

    return get_settings().database_url


def run_migrations_offline() -> None:
    """Генерация SQL без подключения к БД (``alembic upgrade --sql``)."""

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


async def run_migrations_online() -> None:
    """Обычный путь: реальное асинхронное подключение к PostgreSQL."""

    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
