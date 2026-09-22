"""Очередь исходящих: захват-лизинг, отправка, backoff (BOT-001)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from upravdom.bot_gateway import outbox
from upravdom.bot_gateway.max_client import FakeMaxClient

pytestmark = pytest.mark.asyncio


async def test_enqueue_then_claim(session: AsyncSession) -> None:
    await outbox.enqueue_message(session, chat_id="chat-1", text_="привет")

    claimed = await outbox.claim_batch(session, batch_size=10)

    assert len(claimed) == 1
    assert claimed[0].max_chat_id == "chat-1"
    assert claimed[0].text == "привет"
    assert claimed[0].attempts == 1


async def test_claimed_message_not_claimed_again_within_lease(session: AsyncSession) -> None:
    await outbox.enqueue_message(session, chat_id="chat-1", text_="hi")
    first = await outbox.claim_batch(session, batch_size=10)
    second = await outbox.claim_batch(session, batch_size=10)

    assert len(first) == 1
    assert len(second) == 0  # лизинг ещё не истёк


async def test_send_claimed_success_marks_sent(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    await outbox.enqueue_message(session, chat_id="chat-1", text_="hi")
    [message] = await outbox.claim_batch(session, batch_size=10)
    client = FakeMaxClient()

    await outbox.send_claimed(
        session, message, client=client, max_attempts=5, backoff_base_seconds=1.0
    )

    result = await connection.execute(
        text("SELECT status FROM outbound_messages WHERE id = :id"), {"id": message.id}
    )
    assert result.scalar_one() == "sent"
    assert client.sent == [("chat-1", "hi")]


async def test_send_claimed_transient_failure_schedules_retry(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    await outbox.enqueue_message(session, chat_id="chat-1", text_="hi")
    [message] = await outbox.claim_batch(session, batch_size=10)
    client = FakeMaxClient(fail_times=99)

    await outbox.send_claimed(
        session, message, client=client, max_attempts=5, backoff_base_seconds=1.0
    )

    result = await connection.execute(
        text("SELECT status, next_attempt_at FROM outbound_messages WHERE id = :id"),
        {"id": message.id},
    )
    row = result.one()
    assert row.status == "pending"  # не failed — попытки ещё есть
    assert row.next_attempt_at is not None


async def test_send_claimed_exhausted_attempts_marks_failed(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    await outbox.enqueue_message(session, chat_id="chat-1", text_="hi")
    [message] = await outbox.claim_batch(session, batch_size=10)
    client = FakeMaxClient(fail_times=99)

    # message.attempts == 1 после первого claim; имитируем, что это последняя попытка.
    await outbox.send_claimed(
        session, message, client=client, max_attempts=1, backoff_base_seconds=1.0
    )

    result = await connection.execute(
        text("SELECT status FROM outbound_messages WHERE id = :id"), {"id": message.id}
    )
    assert result.scalar_one() == "failed"


async def test_send_claimed_permanent_failure_marks_failed_immediately(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    await outbox.enqueue_message(session, chat_id="chat-1", text_="hi")
    [message] = await outbox.claim_batch(session, batch_size=10)
    client = FakeMaxClient(permanent_failure=True)

    await outbox.send_claimed(
        session, message, client=client, max_attempts=5, backoff_base_seconds=1.0
    )

    result = await connection.execute(
        text("SELECT status FROM outbound_messages WHERE id = :id"), {"id": message.id}
    )
    assert result.scalar_one() == "failed"


async def test_message_recovers_after_lease_expires(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    """Сбой отправки не теряет сообщение: оно доступно к повтору после backoff."""

    await outbox.enqueue_message(session, chat_id="chat-1", text_="hi")
    [message] = await outbox.claim_batch(session, batch_size=10)
    client = FakeMaxClient(fail_times=1)
    await outbox.send_claimed(
        session, message, client=client, max_attempts=5, backoff_base_seconds=1.0
    )

    # Двигаем next_attempt_at в прошлое — имитация "backoff истёк".
    await connection.execute(
        text(
            "UPDATE outbound_messages "
            "SET next_attempt_at = now() - interval '1 second' WHERE id = :id"
        ),
        {"id": message.id},
    )

    [reclaimed] = await outbox.claim_batch(session, batch_size=10)
    await outbox.send_claimed(
        session, reclaimed, client=client, max_attempts=5, backoff_base_seconds=1.0
    )

    result = await connection.execute(
        text("SELECT status FROM outbound_messages WHERE id = :id"), {"id": message.id}
    )
    assert result.scalar_one() == "sent"
    assert client.sent == [("chat-1", "hi")]
