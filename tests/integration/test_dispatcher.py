"""Диспетчер: подтверждение приёма и ограничение частоты (BOT-001).

Payload — по подтверждённой MAX-001 схеме `Update` (max_api.md §5), не
Telegram-подобной догадке из первой версии.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from upravdom.bot_gateway.dispatcher import (
    ACKNOWLEDGEMENT_TEXT,
    RATE_LIMIT_TEXT,
    Dispatcher,
    acknowledge_receipt,
)
from upravdom.bot_gateway.inbox import ClaimedEvent
from upravdom.bot_gateway.rate_limit import RateLimiter

pytestmark = pytest.mark.asyncio


def _event(*, max_user_id: str = "1", chat_id: str = "1", text_: str = "нет света") -> ClaimedEvent:
    return ClaimedEvent(
        id=uuid.uuid4(),
        max_event_id=f"evt-{uuid.uuid4()}",
        payload={
            "update_type": "message_created",
            "timestamp": 1700000000000,
            "chat_id": chat_id,
            "message": {
                "sender": {"user_id": max_user_id},
                "recipient": {"chat_id": chat_id},
                "body": {"mid": f"mid-{uuid.uuid4()}", "text": text_},
            },
        },
        attempts=1,
    )


def _dispatcher_with_acknowledge(*, max_per_minute: int) -> Dispatcher:
    dispatcher = Dispatcher(rate_limiter=RateLimiter(max_per_minute=max_per_minute))
    dispatcher.register("message_created", acknowledge_receipt)
    return dispatcher


async def test_acknowledge_receipt_enqueues_message_and_creates_user(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    dispatcher = _dispatcher_with_acknowledge(max_per_minute=100)
    event = _event(max_user_id="max-42", chat_id="chat-42")

    await dispatcher.dispatch(session, event)

    outbound = await connection.execute(
        text("SELECT max_chat_id, payload FROM outbound_messages WHERE max_chat_id = 'chat-42'")
    )
    row = outbound.one()
    assert row.payload["text"] == ACKNOWLEDGEMENT_TEXT

    user = await connection.execute(
        text("SELECT max_user_id FROM users WHERE max_user_id = 'max-42'")
    )
    assert user.scalar_one() == "max-42"


async def test_unregistered_event_type_is_silent_noop(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    """Системное событие без зарегистрированного обработчика — тишина, не ack."""

    dispatcher = _dispatcher_with_acknowledge(max_per_minute=100)
    event = ClaimedEvent(
        id=uuid.uuid4(),
        max_event_id="evt-system",
        payload={"update_type": "chat_title_changed", "timestamp": 1, "chat_id": "chat-99"},
        attempts=1,
    )

    await dispatcher.dispatch(session, event)

    outbound = await connection.execute(
        text("SELECT count(*) FROM outbound_messages WHERE max_chat_id = 'chat-99'")
    )
    assert outbound.scalar_one() == 0


async def test_custom_handler_registered_for_event_type(session: AsyncSession) -> None:
    dispatcher = Dispatcher(rate_limiter=RateLimiter(max_per_minute=100))
    called = []

    async def custom_handler(_session: AsyncSession, event: ClaimedEvent) -> None:
        called.append(event.max_event_id)

    dispatcher.register("message_created", custom_handler)
    event = _event()

    await dispatcher.dispatch(session, event)

    assert called == [event.max_event_id]


async def test_rate_limited_message_does_not_reach_handler(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    dispatcher = Dispatcher(rate_limiter=RateLimiter(max_per_minute=1))
    handler_calls = []

    async def counting_handler(_session: AsyncSession, event: ClaimedEvent) -> None:
        handler_calls.append(event.max_event_id)

    dispatcher.register("message_created", counting_handler)

    first = _event(max_user_id="max-limited", chat_id="chat-limited")
    second = _event(max_user_id="max-limited", chat_id="chat-limited")

    await dispatcher.dispatch(session, first)
    await dispatcher.dispatch(session, second)

    assert handler_calls == [first.max_event_id]  # второе сообщение не дошло

    outbound = await connection.execute(
        text(
            "SELECT payload FROM outbound_messages WHERE max_chat_id = 'chat-limited' "
            "ORDER BY created_at"
        )
    )
    texts = [row.payload["text"] for row in outbound]
    assert texts == [RATE_LIMIT_TEXT]
