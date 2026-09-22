"""last_error в очередях не содержит текст обращения (MASK-001 / §11)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from upravdom.bot_gateway.dispatcher import Dispatcher
from upravdom.bot_gateway.inbox import ClaimedEvent, record_inbound_event
from upravdom.bot_gateway.rate_limit import RateLimiter
from upravdom.config import get_settings
from upravdom.db import session_scope
from upravdom.scheduler import process_inbound_batch

pytestmark = pytest.mark.asyncio

RAW_SECRET = "СЕКРЕТНЫЙ_ТЕКСТ_ЖИТЕЛЯ_кв42_Иванов_+79171234567"


@pytest.fixture
async def cleanup_events() -> list[str]:
    """Совместимо с test_scheduler: события живут в test DB до отката сессии фикстур."""

    return []


async def test_handler_exception_with_raw_text_does_not_leak_into_last_error(
    cleanup_events: list[str],
) -> None:
    """Обработчик бросает исключение с текстом жителя — в last_error только тип."""

    max_event_id = f"mask-err-{uuid.uuid4()}"
    chat_id = f"chat-mask-{uuid.uuid4()}"
    cleanup_events.append(max_event_id)

    payload: dict[str, Any] = {
        "update_type": "message_created",
        "timestamp": 1_758_900_000_000,
        "message": {
            "sender": {"user_id": chat_id},
            "recipient": {"chat_id": chat_id},
            "body": {"mid": max_event_id, "text": RAW_SECRET},
        },
    }
    async with session_scope() as session:
        await record_inbound_event(
            session, max_event_id=max_event_id, payload=payload, max_user_id=chat_id
        )

    async def boom(_session: object, event: ClaimedEvent) -> None:
        raise RuntimeError(f"failed while processing: {event.payload}")

    dispatcher = Dispatcher(rate_limiter=RateLimiter(max_per_minute=1000))
    dispatcher.register("message_created", boom)

    settings = get_settings()
    for _ in range(settings.inbound_max_attempts):
        await process_inbound_batch(dispatcher=dispatcher)

    async with session_scope() as session:
        row = (
            await session.execute(
                text("SELECT last_error, status FROM inbound_events WHERE max_event_id = :e"),
                {"e": max_event_id},
            )
        ).one()
        last_error, status = row[0], row[1]

    assert status == "failed"
    assert last_error == "RuntimeError"
    assert RAW_SECRET not in (last_error or "")
    assert "Иванов" not in (last_error or "")
    assert "79171234567" not in (last_error or "")
    assert "кв42" not in (last_error or "")

    # process_inbound_batch коммитит в общую test-БД (не в savepoint фикстуры) —
    # убрать хвост, иначе outbox-тесты забирают чужие pending.
    async with session_scope() as session:
        await session.execute(
            text("DELETE FROM outbound_messages WHERE max_chat_id = :c"),
            {"c": chat_id},
        )
        await session.execute(
            text("DELETE FROM inbound_events WHERE max_event_id = :e"),
            {"e": max_event_id},
        )
        await session.commit()
