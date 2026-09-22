"""Идемпотентность и конкурентный захват inbox на реальном PostgreSQL (BOT-001).

Критерии приёмки: одна доставка события трижды → одна запись; два
конкурентных воркера не обрабатывают одно и то же событие дважды;
зависшее в `processing` событие восстанавливается после "падения" воркера.

Большинство тестов используют фикстуры `session`/`connection` (откат в
конце — см. `conftest.py`, `join_transaction_mode="create_savepoint"`).
Тест конкурентного захвата — исключение: ему нужны два **настоящих**
соединения, видящих закоммиченные данные друг друга, поэтому он не может
жить внутри одной откатываемой транзакции и убирает за собой сам.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from upravdom.bot_gateway import inbox
from upravdom.config import get_settings

pytestmark = pytest.mark.asyncio


async def test_triple_delivery_creates_one_row(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    payload = {"update_id": 1, "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "x"}}

    for _ in range(3):
        await inbox.record_inbound_event(session, max_event_id="evt-triple", payload=payload)

    result = await connection.execute(
        text("SELECT count(*) FROM inbound_events WHERE max_event_id = 'evt-triple'")
    )
    assert result.scalar_one() == 1


async def test_stale_processing_event_is_reclaimed(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    event_id = uuid.uuid4()
    stale_start = datetime.now(UTC) - timedelta(minutes=10)
    await connection.execute(
        text(
            "INSERT INTO inbound_events "
            "(id, max_event_id, payload, status, attempts, received_at, processing_started_at) "
            "VALUES (:id, 'evt-stale', '{}', 'processing', 1, now(), :started)"
        ),
        {"id": event_id, "started": stale_start},
    )

    await inbox.reclaim_stale_processing(
        session, visibility_timeout=timedelta(seconds=120), max_attempts=5
    )

    result = await connection.execute(
        text("SELECT status FROM inbound_events WHERE id = :id"), {"id": event_id}
    )
    assert result.scalar_one() == "pending"


async def test_stale_processing_event_fails_after_max_attempts(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    event_id = uuid.uuid4()
    stale_start = datetime.now(UTC) - timedelta(minutes=10)
    await connection.execute(
        text(
            "INSERT INTO inbound_events "
            "(id, max_event_id, payload, status, attempts, received_at, processing_started_at) "
            "VALUES (:id, 'evt-stale-exhausted', '{}', 'processing', 5, now(), :started)"
        ),
        {"id": event_id, "started": stale_start},
    )

    await inbox.reclaim_stale_processing(
        session, visibility_timeout=timedelta(seconds=120), max_attempts=5
    )

    result = await connection.execute(
        text("SELECT status FROM inbound_events WHERE id = :id"), {"id": event_id}
    )
    assert result.scalar_one() == "failed"


async def test_mark_failed_attempt_retries_until_exhausted(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    event_id = uuid.uuid4()
    await connection.execute(
        text(
            "INSERT INTO inbound_events (id, max_event_id, payload, status, attempts, received_at) "
            "VALUES (:id, 'evt-fail', '{}', 'processing', 1, now())"
        ),
        {"id": event_id},
    )

    is_final = await inbox.mark_failed_attempt(
        session, event_id, attempts=1, max_attempts=5, error="BoomError"
    )
    assert is_final is False

    is_final = await inbox.mark_failed_attempt(
        session, event_id, attempts=5, max_attempts=5, error="BoomError"
    )
    assert is_final is True

    result = await connection.execute(
        text("SELECT status, last_error FROM inbound_events WHERE id = :id"), {"id": event_id}
    )
    row = result.one()
    assert row.status == "failed"
    assert row.last_error == "BoomError"


async def test_get_or_create_user_is_idempotent(session: AsyncSession) -> None:
    first = await inbox.get_or_create_user(session, "max-user-1")
    second = await inbox.get_or_create_user(session, "max-user-1")

    assert first.id == second.id


async def test_concurrent_claim_does_not_double_claim() -> None:
    """Два независимых воркера (два реальных соединения) не берут одну строку.

    Вне общей откатываемой транзакции: `FOR UPDATE SKIP LOCKED` между двумя
    соединениями виден только если данные реально закоммичены.
    """

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    event_ids = [uuid.uuid4() for _ in range(10)]
    try:
        async with engine.begin() as setup_conn:
            for i, event_id in enumerate(event_ids):
                await setup_conn.execute(
                    text(
                        "INSERT INTO inbound_events "
                        "(id, max_event_id, payload, status, attempts, received_at) "
                        "VALUES (:id, :m, '{}', 'pending', 0, now())"
                    ),
                    {"id": event_id, "m": f"evt-concurrent-{i}"},
                )

        async def claim_worker() -> list[str]:
            async with engine.connect() as conn:
                worker_session = async_sessionmaker(bind=conn, expire_on_commit=False)()
                claimed = await inbox.claim_batch(worker_session, batch_size=6)
                return [c.max_event_id for c in claimed]

        try:
            results = await asyncio.gather(claim_worker(), claim_worker())
            all_claimed = results[0] + results[1]
            assert len(all_claimed) == len(set(all_claimed)), "одно событие захвачено дважды"
            assert len(all_claimed) == 10
        finally:
            async with engine.begin() as cleanup_conn:
                await cleanup_conn.execute(
                    text("DELETE FROM inbound_events WHERE id = ANY(:ids)"),
                    {"ids": event_ids},
                )
    finally:
        await engine.dispose()
