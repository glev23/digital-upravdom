"""Фикстуры интеграционных тестов: реальный PostgreSQL, откат после теста.

Требует поднятого `docker compose up -d postgres` (DATABASE_URL из `.env`
указывает на хостовый порт — architecture.md §12.1, дев-воркфлоу через venv).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from scripts.seed_demo import seed
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from upravdom.bot_gateway.inbox import get_or_create_user
from upravdom.config import get_settings
from upravdom.db import get_engine, get_session_factory
from upravdom.onboarding import service as onboarding_service

# Интеграционные тесты работают в отдельной базе `<имя>_test`, а не в базе
# разработки. Причина — реальный сбой: запущенный `docker compose up` app
# опрашивает ту же базу воркером inbox раз в секунду и забирал `pending`-события,
# вставленные тестом (`test_concurrent_claim_does_not_double_claim` падал ~1 из 5
# только при работающем контейнере; при остановленном — 15/15). Подмена — на
# уровне модуля: до того, как тестовые модули впервые прочитают настройки.
_dev_url = make_url(get_settings().database_url)
if _dev_url.database and not _dev_url.database.endswith("_test"):
    os.environ["DATABASE_URL"] = _dev_url.set(
        database=f"{_dev_url.database}_test"
    ).render_as_string(hide_password=False)
    get_settings.cache_clear()

# Намеренно НЕ используем `upravdom.db.get_engine()`: он закэширован через
# `lru_cache` на уровень процесса, а `pytest-asyncio` в function-scope даёт
# каждому тесту собственный event loop — соединение asyncpg, созданное в
# loop первого теста, не переживает переход ко второму ("Event loop is
# closed"). Отдельный движок на тест снимает эту зависимость от scope loop.


async def _ensure_test_database(url: str) -> None:
    target = make_url(url)
    admin = create_async_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target.database}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> None:
    """Создаёт тестовую базу (см. подмену `DATABASE_URL` выше) и накатывает миграции."""

    asyncio.run(_ensure_test_database(get_settings().database_url))
    alembic_cfg = Config(str(Path(__file__).resolve().parents[2] / "db" / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")


@pytest.fixture
async def connection() -> AsyncIterator[AsyncConnection]:
    """Соединение в незакоммиченной транзакции — откатывается после теста."""

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                yield conn
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Сессия поверх той же транзакции, что и `connection`.

    `join_transaction_mode="create_savepoint"` обязателен: код под тестом
    (`inbox.py`/`outbox.py`) сам вызывает `session.commit()` — без этого
    режима такой commit закоммитил бы внешнюю транзакцию фикстуры целиком,
    и откат в `connection` после теста был бы уже нечего откатывать (а на
    части версий SQLAlchemy — ещё и падал бы с ошибкой на уже закрытой
    транзакции). С `create_savepoint` внутренний commit фиксирует только
    SAVEPOINT, а откат внешней транзакции по-прежнему отменяет всё.
    """

    session_factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with session_factory() as s:
        yield s


@pytest.fixture(autouse=True)
def _reset_process_engine_cache() -> Iterator[None]:
    """Сбрасывает `upravdom.db.get_engine()` между тестами.

    Код приложения (`scheduler.py`, `db.check_database_ready` и т.п.)
    сознательно кэширует движок на уровень процесса через `lru_cache` —
    это правильное поведение для рантайма с одним постоянным event loop.
    Но `pytest-asyncio` в function-scope даёт каждому тесту свой event
    loop, и без сброса второй тест, вызвавший `session_scope()`, получил
    бы движок с соединением от event loop первого теста ("Event loop is
    closed"). Пересоздание не требуется на каждый запрос — было бы
    избыточно и не отражало бы реальное поведение приложения; сброс
    ровно между тестами достаточен и не маскирует багов кода под тестом.
    """

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield
    get_engine.cache_clear()
    get_session_factory.cache_clear()


OnboardUser = Callable[[AsyncSession, str], Awaitable[uuid.UUID]]


@pytest.fixture
def onboard_user() -> OnboardUser:
    """Житель, уже прошедший онбординг: демо-дом привязан, согласие выдано.

    Коммитит — для тестов воркера, который открывает собственные сессии.
    """

    async def _onboard(session: AsyncSession, max_user_id: str) -> uuid.UUID:
        deep_links = await seed(session)
        token = next(iter(deep_links.values()))[0]
        link = await onboarding_service.resolve_token(session, token)
        assert link is not None
        user = await get_or_create_user(session, max_user_id)
        await onboarding_service.bind_house(session, user.id, link.house.id)
        await onboarding_service.grant_consent(
            session, user.id, version=get_settings().consent_version, source="test"
        )
        await session.commit()
        return user.id

    return _onboard
