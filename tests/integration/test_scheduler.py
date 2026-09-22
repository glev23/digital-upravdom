"""Сквозное поведение воркеров inbox/outbox (BOT-001, критерии приёмки).

Использует настоящий отдельный движок (не фикстуру `session`/`connection`):
`process_inbound_batch`/`process_outbound_batch` сами открывают сессии через
`upravdom.db.session_scope()`, которая берёт `DATABASE_URL` из настроек —
эти тесты полагаются на реально закоммиченные данные, а не на откатываемую
транзакцию теста.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from upravdom.bot_gateway.dispatcher import ACKNOWLEDGEMENT_TEXT, Dispatcher
from upravdom.bot_gateway.inbox import ClaimedEvent, record_inbound_event
from upravdom.bot_gateway.rate_limit import RateLimiter
from upravdom.config import get_settings
from upravdom.db import session_scope
from upravdom.scheduler import process_inbound_batch, process_outbound_batch

pytestmark = pytest.mark.asyncio

OnboardUser = Callable[[AsyncSession, str], Awaitable[uuid.UUID]]


@pytest.fixture
async def cleanup_events() -> AsyncIterator[list[str]]:
    """Список `max_event_id`, которые нужно удалить после теста (реальные коммиты)."""

    ids: list[str] = []
    yield ids
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            for event_id in ids:
                await conn.execute(
                    text("DELETE FROM inbound_events WHERE max_event_id = :m"), {"m": event_id}
                )
                await conn.execute(
                    text("DELETE FROM outbound_messages WHERE max_chat_id = :c"),
                    {"c": event_id},
                )
                for table in ("consents", "user_houses"):
                    await conn.execute(
                        text(
                            f"DELETE FROM {table} WHERE user_id IN "
                            "(SELECT id FROM users WHERE max_user_id = :c)"
                        ),
                        {"c": event_id},
                    )
                await conn.execute(
                    text("DELETE FROM users WHERE max_user_id = :c"), {"c": event_id}
                )
    finally:
        await engine.dispose()


async def test_inbound_event_processed_end_to_end(
    cleanup_events: list[str], onboard_user: OnboardUser
) -> None:
    """Житель с привязанным домом и согласием — сообщение проходит шлюз ONBOARD-001.

    Диспетчер с `acknowledge_receipt` — проверка воркера inbox (BOT-001), не FLOW.
    """

    from upravdom.bot_gateway.dispatcher import acknowledge_receipt
    from upravdom.onboarding.handlers import gated

    max_event_id = f"evt-e2e-{uuid.uuid4()}"
    chat_id = f"chat-{uuid.uuid4()}"
    cleanup_events.append(max_event_id)
    cleanup_events.append(chat_id)  # переиспользуем список для очистки outbound/users тоже

    payload: dict[str, object] = {
        "update_type": "message_created",
        "timestamp": 1700000000000,
        "chat_id": chat_id,
        "message": {
            "sender": {"user_id": chat_id},
            "recipient": {"chat_id": chat_id},
            "body": {"mid": max_event_id, "text": "нет воды"},
        },
    }
    async with session_scope() as session:
        await onboard_user(session, chat_id)
        await record_inbound_event(
            session, max_event_id=max_event_id, payload=payload, max_user_id=chat_id
        )

    dispatcher = Dispatcher()
    dispatcher.register("message_created", gated(acknowledge_receipt))
    await process_inbound_batch(dispatcher=dispatcher)

    async with session_scope() as session:
        result = await session.execute(
            text("SELECT status FROM inbound_events WHERE max_event_id = :m"),
            {"m": max_event_id},
        )
        assert result.scalar_one() == "done"

        outbound = await session.execute(
            text("SELECT payload FROM outbound_messages WHERE max_chat_id = :c"),
            {"c": chat_id},
        )
        assert outbound.scalar_one()["text"] == ACKNOWLEDGEMENT_TEXT


async def test_always_failing_handler_exhausts_retries_and_notifies(
    cleanup_events: list[str],
) -> None:
    max_event_id = f"evt-fail-{uuid.uuid4()}"
    chat_id = f"chat-fail-{uuid.uuid4()}"
    cleanup_events.append(max_event_id)
    cleanup_events.append(chat_id)

    payload: dict[str, object] = {
        "update_type": "message_created",
        "timestamp": 1700000000000,
        "chat_id": chat_id,
        "message": {
            "sender": {"user_id": chat_id},
            "recipient": {"chat_id": chat_id},
            "body": {"mid": max_event_id, "text": "test"},
        },
    }
    async with session_scope() as session:
        await record_inbound_event(session, max_event_id=max_event_id, payload=payload)

    async def always_fails(_session: object, _event: ClaimedEvent) -> None:
        raise RuntimeError("boom")

    dispatcher = Dispatcher(rate_limiter=RateLimiter(max_per_minute=1000))
    dispatcher.register("message_created", always_fails)

    settings = get_settings()
    # inbound_max_attempts попыток: каждый вызов process_inbound_batch — одна попытка.
    for _ in range(settings.inbound_max_attempts):
        await process_inbound_batch(dispatcher=dispatcher)

    async with session_scope() as session:
        result = await session.execute(
            text("SELECT status, attempts FROM inbound_events WHERE max_event_id = :m"),
            {"m": max_event_id},
        )
        row = result.one()
        assert row.status == "failed"
        assert row.attempts == settings.inbound_max_attempts

        outbound = await session.execute(
            text("SELECT payload FROM outbound_messages WHERE max_chat_id = :c"),
            {"c": chat_id},
        )
        payloads = [r["text"] for r in outbound.scalars()]
        assert any("сбой" in p for p in payloads)


async def test_outbound_worker_sends_via_client(cleanup_events: list[str]) -> None:
    from upravdom.bot_gateway import outbox
    from upravdom.bot_gateway.max_client import FakeMaxClient

    chat_id = f"chat-outbound-{uuid.uuid4()}"
    cleanup_events.append(chat_id)

    async with session_scope() as session:
        await outbox.enqueue_message(session, chat_id=chat_id, text_="hello")
        await session.commit()

    client = FakeMaxClient()
    await process_outbound_batch(client=client)

    assert (chat_id, "hello") in client.sent


async def test_outbound_worker_noop_without_client(cleanup_events: list[str]) -> None:
    """Без клиента (нет токена/адреса) воркер не падает и не теряет сообщение."""

    from upravdom.bot_gateway import outbox

    chat_id = f"chat-noclient-{uuid.uuid4()}"
    cleanup_events.append(chat_id)

    async with session_scope() as session:
        await outbox.enqueue_message(session, chat_id=chat_id, text_="hello")
        await session.commit()

    await process_outbound_batch(client=None)  # не должно бросить исключение

    async with session_scope() as session:
        result = await session.execute(
            text("SELECT status FROM outbound_messages WHERE max_chat_id = :c"),
            {"c": chat_id},
        )
        assert result.scalar_one() == "pending"  # осталось в очереди, не потеряно


async def test_failure_mid_onboarding_leaves_no_partial_state(
    cleanup_events: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой после привязки дома, но до согласия — откатывается всё (onboard-001.md)."""

    from scripts.seed_demo import seed

    from upravdom.onboarding import service
    from upravdom.onboarding.callbacks import Action, OnboardingCallback, RefKind, encode

    user = f"u-atomic-{uuid.uuid4()}"
    cleanup_events.append(user)

    async with session_scope() as session:
        token = next(iter((await seed(session)).values()))[0]
        await session.commit()

    payload: dict[str, object] = {
        "update_type": "message_callback",
        "timestamp": 1758550000000,
        "callback": {
            "timestamp": 1758550000000,
            "callback_id": f"cb-{uuid.uuid4()}",
            "payload": encode(
                OnboardingCallback(
                    Action.ACCEPT, RefKind.TOKEN, token, get_settings().consent_version
                )
            ),
            "user": {"user_id": user},
        },
        "message": {"recipient": {"chat_id": user}},
    }
    max_event_id = f"message_callback:{payload['callback']['callback_id']}:1758550000000"  # type: ignore[index]
    cleanup_events.append(max_event_id)
    async with session_scope() as session:
        await record_inbound_event(
            session, max_event_id=max_event_id, payload=payload, max_user_id=user
        )

    async def broken_grant(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("storage hiccup")

    monkeypatch.setattr(service, "grant_consent", broken_grant)
    await process_inbound_batch()

    async with session_scope() as session:
        bound = await session.scalar(
            text(
                "SELECT count(*) FROM user_houses uh JOIN users u ON u.id = uh.user_id "
                "WHERE u.max_user_id = :u"
            ),
            {"u": user},
        )
        assert bound == 0
        queued = await session.scalar(
            text("SELECT count(*) FROM outbound_messages WHERE max_chat_id = :c"), {"c": user}
        )
        assert queued == 0
        status = await session.scalar(
            text("SELECT status FROM inbound_events WHERE max_event_id = :m"),
            {"m": max_event_id},
        )
        assert status == "pending"  # будет повторено, а не потеряно
