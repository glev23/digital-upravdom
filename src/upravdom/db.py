"""Асинхронное подключение к PostgreSQL.

Движок, фабрика сессий и проверка доступности для `/ready` (INIT-001) плюс
FastAPI-зависимость `get_session` для эндпоинтов (BOT-001).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from upravdom.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Один движок на процесс; пересоздаётся только при смене кэша настроек."""

    settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=get_engine(), expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Короткоживущая сессия с гарантированным закрытием."""

    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: `Depends(get_session)` — сессия на один запрос."""

    async with session_scope() as session:
        yield session


async def check_database_ready() -> bool:
    """Возвращает True, если PostgreSQL отвечает на простой запрос."""

    try:
        engine = get_engine()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — любой сбой хранилища означает "не готов"
        return False
