"""Запасной путь: поиск дома по адресу (сценарии 1–11, ONBOARD-002)."""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest
from scripts.seed_demo import HOUSES, seed
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway.dispatcher import acknowledge_receipt
from upravdom.bot_gateway.inbox import ClaimedEvent
from upravdom.models import Consent, HouseRequest, User, UserHouse
from upravdom.onboarding import texts
from upravdom.onboarding.callbacks import Action, OnboardingCallback, RefKind, decode, encode
from upravdom.onboarding.handlers import gated, on_bot_started, on_callback

_ts = itertools.count(1_758_560_000_000, 1000)
MESSAGE = gated(acknowledge_receipt)
ADDRESS_1 = HOUSES[0]["address_raw"]


def _message(user: str, text_: str) -> dict[str, Any]:
    return {
        "update_type": "message_created",
        "timestamp": next(_ts),
        "message": {
            "sender": {"user_id": user},
            "recipient": {"chat_id": user},
            "body": {"mid": f"mid-{uuid.uuid4()}", "text": text_},
        },
    }


def _bot_started(user: str, token: str | None = None) -> dict[str, Any]:
    return {
        "update_type": "bot_started",
        "timestamp": next(_ts),
        "chat_id": user,
        "user": {"user_id": user},
        "payload": token,
    }


def _callback(user: str, payload: str) -> dict[str, Any]:
    ts = next(_ts)
    return {
        "update_type": "message_callback",
        "timestamp": ts,
        "callback": {
            "timestamp": ts,
            "callback_id": f"cb-{uuid.uuid4()}",
            "payload": payload,
            "user": {"user_id": user},
        },
        "message": {"recipient": {"chat_id": user}},
    }


async def _deliver(session: AsyncSession, payload: dict[str, Any], handler: Any) -> uuid.UUID:
    from upravdom.bot_gateway import inbox
    from upravdom.bot_gateway.schemas import parse_webhook_payload

    parsed = parse_webhook_payload(payload)
    await inbox.record_inbound_event(
        session, max_event_id=parsed.event_id, payload=payload, max_user_id=parsed.max_user_id
    )
    event_id = await session.scalar(
        text(
            "UPDATE inbound_events SET status = 'processing', attempts = 1 "
            "WHERE max_event_id = :m RETURNING id"
        ),
        {"m": parsed.event_id},
    )
    assert event_id is not None
    await handler(session, ClaimedEvent(event_id, parsed.event_id, payload, 1))
    await inbox.mark_done(session, event_id)
    return uuid.UUID(str(event_id))


async def _outbox(session: AsyncSession, chat: str) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT payload FROM outbound_messages WHERE max_chat_id = :c "
            "AND NOT (payload::jsonb ? 'callback_id') ORDER BY created_at, id"
        ),
        {"c": chat},
    )
    return [r[0] for r in rows]


async def _last(session: AsyncSession, chat: str) -> dict[str, Any]:
    messages = await _outbox(session, chat)
    assert messages, "бот ничего не ответил"
    return messages[-1]


def _buttons(message: dict[str, Any]) -> list[OnboardingCallback]:
    rows = message["attachments"][0]["payload"]["buttons"]
    decoded = [decode(b["payload"]) for row in rows for b in row]
    return [cb for cb in decoded if cb is not None]


def _button(message: dict[str, Any], action: Action) -> str:
    return encode(next(cb for cb in _buttons(message) if cb.action is action))


@pytest.fixture
async def demo(session: AsyncSession) -> None:
    await seed(session)


@pytest.fixture
def user() -> str:
    return f"u-{uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize(
    "query", ["Декабристов 10", "г. Казань, ул. Декабристов, д. 10", "декабрисов 10"]
)
async def test_search_finds_decabristov_and_consent_source(
    session: AsyncSession, demo: None, user: str, query: str
) -> None:
    await _deliver(session, _message(user, query), MESSAGE)
    choices = await _last(session, user)
    assert choices["text"] == texts.ADDRESS_CHOICES

    await _deliver(session, _callback(user, _button(choices, Action.PICK)), on_callback)
    prompt = await _last(session, user)
    assert ADDRESS_1 in prompt["text"]
    accept = next(cb for cb in _buttons(prompt) if cb.action is Action.ACCEPT)
    assert accept.ref_kind is RefKind.SEARCH

    await _deliver(session, _callback(user, encode(accept)), on_callback)

    uid = await session.scalar(select(User.id).where(User.max_user_id == user))
    assert uid is not None
    consent = (await session.scalars(select(Consent).where(Consent.user_id == uid))).one()
    assert consent.source == "address_search"
    assert (
        (await session.scalars(select(UserHouse).where(UserHouse.user_id == uid))).one().is_primary
    )
    assert (await _last(session, user))["text"] == texts.ONBOARDED


async def test_search_finds_lumumba(session: AsyncSession, demo: None, user: str) -> None:
    await _deliver(session, _message(user, "Лумумбы 5"), MESSAGE)
    choices = await _last(session, user)
    assert HOUSES[1]["address_raw"] in choices["attachments"][0]["payload"]["buttons"][0][0]["text"]


async def test_wrong_house_number_goes_to_not_found(
    session: AsyncSession, demo: None, user: str
) -> None:
    await _deliver(session, _message(user, "Декабристов 12"), MESSAGE)
    reply = await _last(session, user)
    assert "не подключён" in reply["text"]
    actions = {cb.action for cb in _buttons(reply)}
    assert actions == {Action.RETRY, Action.LEAVE}


async def test_unknown_street_not_found_and_leave_address(
    session: AsyncSession, demo: None, user: str
) -> None:
    await _deliver(session, _message(user, "Пушкина 1"), MESSAGE)
    reply = await _last(session, user)
    assert texts.BTN_LEAVE_ADDRESS in [
        b["text"] for row in reply["attachments"][0]["payload"]["buttons"] for b in row
    ]

    await _deliver(session, _callback(user, _button(reply, Action.LEAVE)), on_callback)

    uid = await session.scalar(select(User.id).where(User.max_user_id == user))
    req = (await session.scalars(select(HouseRequest).where(HouseRequest.user_id == uid))).one()
    assert req.address_text == "Пушкина 1"
    assert req.status.value == "new"
    assert "Пушкина 1" in (await _last(session, user))["text"]


async def test_garbage_asks_format(session: AsyncSession, demo: None, user: str) -> None:
    await _deliver(session, _message(user, "привет"), MESSAGE)
    assert (await _last(session, user))["text"] == texts.ASK_ADDRESS_HINT


async def test_bot_started_without_payload_asks_address(
    session: AsyncSession, demo: None, user: str
) -> None:
    await _deliver(session, _bot_started(user, None), on_bot_started)
    assert (await _last(session, user))["text"] == texts.NO_LINK


async def test_wrong_house_button_asks_address(
    session: AsyncSession, demo: None, user: str
) -> None:
    token = (
        await session.execute(text("SELECT token FROM house_links WHERE label = 'подъезд' LIMIT 1"))
    ).scalar_one()
    await _deliver(session, _bot_started(user, token), on_bot_started)
    prompt = await _last(session, user)
    await _deliver(session, _callback(user, _button(prompt, Action.WRONG_HOUSE)), on_callback)
    assert (await _last(session, user))["text"] == texts.WRONG_HOUSE


async def test_not_in_list_has_continuation(session: AsyncSession, demo: None, user: str) -> None:
    await _deliver(session, _message(user, "Декабристов 10"), MESSAGE)
    choices = await _last(session, user)
    await _deliver(session, _callback(user, _button(choices, Action.NONE_MATCH)), on_callback)
    reply = await _last(session, user)
    actions = {cb.action for cb in _buttons(reply)}
    assert Action.RETRY in actions and Action.LEAVE in actions


async def test_retry_prompts_again(session: AsyncSession, demo: None, user: str) -> None:
    await _deliver(session, _message(user, "Пушкина 1"), MESSAGE)
    not_found = await _last(session, user)
    await _deliver(session, _callback(user, _button(not_found, Action.RETRY)), on_callback)
    assert (await _last(session, user))["text"] == texts.ASK_ADDRESS


async def test_address_not_saved_until_leave_button(
    session: AsyncSession, demo: None, user: str
) -> None:
    await _deliver(session, _message(user, "Пушкина 1"), MESSAGE)
    assert (await session.scalars(select(HouseRequest))).all() == []
