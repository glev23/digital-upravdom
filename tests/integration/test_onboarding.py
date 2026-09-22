"""Онбординг: сценарии 1–12 из onboard-001.md на реальном Postgres.

Обработчики вызываются так же, как их вызывает воркер: событие в статусе
`processing` → обработчик → `mark_done`. Всё внутри откатываемой транзакции
фикстуры `session`.
"""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest
from scripts.seed_demo import HOUSES, seed
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import inbox
from upravdom.bot_gateway.dispatcher import ACKNOWLEDGEMENT_TEXT, acknowledge_receipt
from upravdom.bot_gateway.inbox import ClaimedEvent
from upravdom.config import get_settings
from upravdom.models import Consent, User, UserHouse
from upravdom.onboarding import texts
from upravdom.onboarding.callbacks import Action, OnboardingCallback, RefKind, decode, encode
from upravdom.onboarding.handlers import gated, on_bot_started, on_callback

_ts = itertools.count(1_758_550_000_000, 1000)

ADDRESS_1 = HOUSES[0]["address_raw"]
ADDRESS_2 = HOUSES[1]["address_raw"]


def _bot_started(user: str, token: str | None) -> dict[str, Any]:
    return {
        "update_type": "bot_started",
        "timestamp": next(_ts),
        "chat_id": user,
        "user": {"user_id": user},
        "payload": token,
    }


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
    """Как воркер: строка события в `processing` → обработчик → `mark_done`."""

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


async def _user_id(session: AsyncSession, max_user_id: str) -> uuid.UUID:
    found = await session.scalar(select(User.id).where(User.max_user_id == max_user_id))
    assert found is not None
    return found


async def _event_status(session: AsyncSession, event_id: uuid.UUID) -> tuple[str, int, str | None]:
    row = (
        await session.execute(
            text("SELECT status, attempts, last_error FROM inbound_events WHERE id = :i"),
            {"i": event_id},
        )
    ).one()
    return row.status, row.attempts, row.last_error


@pytest.fixture
async def tokens(session: AsyncSession) -> list[str]:
    """Токены «подъезд» для трёх демо-домов, в порядке HOUSES."""

    links = await seed(session)
    return [links[h["key"]][0] for h in HOUSES]


@pytest.fixture
def user() -> str:
    return f"u-{uuid.uuid4()}"


MESSAGE = gated(acknowledge_receipt)


async def _onboard(session: AsyncSession, user: str, token: str) -> None:
    await _deliver(session, _bot_started(user, token), on_bot_started)
    prompt = await _last(session, user)
    await _deliver(session, _callback(user, _button(prompt, Action.ACCEPT)), on_callback)


# --- 1, 2 -------------------------------------------------------------------


async def test_deep_link_shows_recognized_address_and_consent(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    await _deliver(session, _bot_started(user, tokens[0]), on_bot_started)

    prompt = await _last(session, user)
    assert ADDRESS_1 in prompt["text"]
    assert texts.CONSENT_TEXTS[get_settings().consent_version] in prompt["text"]
    assert {cb.action for cb in _buttons(prompt)} == {
        Action.ACCEPT,
        Action.DECLINE,
        Action.WRONG_HOUSE,
    }
    # До согласия ничего не привязано.
    uid = await _user_id(session, user)
    assert (await session.scalars(select(UserHouse).where(UserHouse.user_id == uid))).all() == []


async def test_accept_binds_house_and_records_consent(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    await _onboard(session, user, tokens[0])

    uid = await _user_id(session, user)
    houses = (await session.scalars(select(UserHouse).where(UserHouse.user_id == uid))).all()
    assert len(houses) == 1 and houses[0].is_primary
    consents = (await session.scalars(select(Consent).where(Consent.user_id == uid))).all()
    assert len(consents) == 1
    assert consents[0].consent_version == get_settings().consent_version
    assert consents[0].source == "deep_link:подъезд"
    assert (await _last(session, user))["text"] == texts.ONBOARDED

    answers = await session.scalar(
        text(
            "SELECT count(*) FROM outbound_messages WHERE max_chat_id = :c "
            "AND payload::jsonb ? 'callback_id'"
        ),
        {"c": user},
    )
    assert answers == 1  # нажатие подтверждено через POST /answers


# --- 3, 4 -------------------------------------------------------------------


async def test_message_without_house_is_address_search_not_hold(
    session: AsyncSession, user: str
) -> None:
    """ONBOARD-002: без дома текст — поиск адреса, не awaiting_consent."""

    event_id = await _deliver(session, _message(user, "течёт стояк"), MESSAGE)

    assert (await _event_status(session, event_id))[0] == "done"
    assert (await _last(session, user))["text"] == texts.ASK_ADDRESS_HINT


async def test_message_with_address_offers_house_choices(session: AsyncSession, user: str) -> None:
    await _deliver(session, _message(user, "Декабристов 10"), MESSAGE)

    reply = await _last(session, user)
    assert reply["text"] == texts.ADDRESS_CHOICES
    picks = [cb for cb in _buttons(reply) if cb.action is Action.PICK]
    assert len(picks) == 1
    assert picks[0].ref_kind is RefKind.SEARCH


async def test_decline_closes_held_messages_without_processing(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    await _onboard(session, user, tokens[0])
    await _deliver(session, _message(user, "/revoke"), MESSAGE)
    held = await _deliver(session, _message(user, "нет света"), MESSAGE)
    prompt = await _last(session, user)
    assert texts.MESSAGE_KEPT in prompt["text"]

    await _deliver(session, _callback(user, _button(prompt, Action.DECLINE)), on_callback)

    status, _, last_error = await _event_status(session, held)
    assert (status, last_error) == ("done", "consent_declined")
    reply = (await _last(session, user))["text"]
    assert reply.startswith(texts.DECLINED)
    assert ACKNOWLEDGEMENT_TEXT not in [m["text"] for m in await _outbox(session, user)]


# --- 5, 6 -------------------------------------------------------------------


async def test_wrong_house_does_not_bind(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    await _deliver(session, _bot_started(user, tokens[0]), on_bot_started)
    prompt = await _last(session, user)

    await _deliver(session, _callback(user, _button(prompt, Action.WRONG_HOUSE)), on_callback)

    assert (await _last(session, user))["text"] == texts.WRONG_HOUSE
    uid = await _user_id(session, user)
    assert (await session.scalars(select(UserHouse).where(UserHouse.user_id == uid))).all() == []


@pytest.mark.parametrize(
    ("token", "expected"),
    [("ffffffffffffffff", texts.UNKNOWN_LINK), (None, texts.NO_LINK), ("", texts.NO_LINK)],
)
async def test_unknown_or_missing_link_is_not_an_error(
    session: AsyncSession, tokens: list[str], user: str, token: str | None, expected: str
) -> None:
    await _deliver(session, _bot_started(user, token), on_bot_started)

    assert (await _last(session, user))["text"] == expected


async def test_revoked_link_is_rejected_even_from_old_button(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    await _deliver(session, _bot_started(user, tokens[0]), on_bot_started)
    prompt = await _last(session, user)
    await session.execute(
        text("UPDATE house_links SET is_active = false, revoked_at = now() WHERE token = :t"),
        {"t": tokens[0]},
    )

    await _deliver(session, _callback(user, _button(prompt, Action.ACCEPT)), on_callback)

    assert (await _last(session, user))["text"] == texts.UNKNOWN_LINK
    uid = await _user_id(session, user)
    assert (await session.scalars(select(Consent).where(Consent.user_id == uid))).all() == []


# --- 7, 8 -------------------------------------------------------------------


async def test_returning_user_passes_silently(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    await _onboard(session, user, tokens[0])

    await _deliver(session, _bot_started(user, tokens[0]), on_bot_started)

    last = await _last(session, user)
    assert last["text"] == texts.ALREADY_ONBOARDED.format(address=ADDRESS_1)
    assert "attachments" not in last


async def test_second_house_link_offers_to_add_address(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    await _onboard(session, user, tokens[0])

    await _deliver(session, _bot_started(user, tokens[1]), on_bot_started)
    offer = await _last(session, user)
    assert ADDRESS_2 in offer["text"] and ADDRESS_1 in offer["text"]

    await _deliver(session, _callback(user, _button(offer, Action.ADD_HOUSE)), on_callback)

    uid = await _user_id(session, user)
    houses = (await session.scalars(select(UserHouse).where(UserHouse.user_id == uid))).all()
    assert sorted(h.is_primary for h in houses) == [False, True]
    assert (await _last(session, user))["text"] == texts.HOUSE_ADDED.format(
        address=ADDRESS_2, primary=ADDRESS_1
    )


# --- 9, 10, 11, 12 ------------------------------------------------------------


async def test_consent_version_bump_asks_again_and_keeps_history(
    session: AsyncSession, tokens: list[str], user: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _onboard(session, user, tokens[0])
    monkeypatch.setitem(texts.CONSENT_TEXTS, 2, "Новый текст согласия v2.")
    monkeypatch.setenv("CONSENT_VERSION", "2")
    get_settings.cache_clear()
    try:
        held = await _deliver(session, _message(user, "нет отопления"), MESSAGE)

        assert (await _event_status(session, held))[0] == "awaiting_consent"
        prompt = await _last(session, user)
        assert "Новый текст согласия v2." in prompt["text"]
        assert texts.MESSAGE_KEPT in prompt["text"]
        accept = next(cb for cb in _buttons(prompt) if cb.action is Action.ACCEPT)
        assert (accept.ref_kind, accept.consent_version) == (RefKind.HOUSE, 2)

        await _deliver(session, _callback(user, encode(accept)), on_callback)

        uid = await _user_id(session, user)
        versions = sorted(
            c.consent_version
            for c in (await session.scalars(select(Consent).where(Consent.user_id == uid))).all()
        )
        assert versions == [1, 2]  # старая запись не удалена
        assert (await _event_status(session, held))[0] == "pending"
    finally:
        get_settings.cache_clear()


async def test_revoke_command(session: AsyncSession, tokens: list[str], user: str) -> None:
    await _onboard(session, user, tokens[0])

    await _deliver(session, _message(user, "/revoke"), MESSAGE)

    uid = await _user_id(session, user)
    consent = (await session.scalars(select(Consent).where(Consent.user_id == uid))).one()
    assert consent.revoked_at is not None  # отзыв — не удаление
    assert (await _last(session, user))["text"] == texts.REVOKED

    held = await _deliver(session, _message(user, "течёт кран"), MESSAGE)
    assert (await _event_status(session, held))[0] == "awaiting_consent"

    await _deliver(session, _message(user, "Отозвать согласие"), MESSAGE)
    assert (await _last(session, user))["text"] == texts.REVOKE_NOTHING


async def test_stale_consent_button_does_not_grant(
    session: AsyncSession, tokens: list[str], user: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _deliver(session, _bot_started(user, tokens[0]), on_bot_started)
    old_prompt = await _last(session, user)
    monkeypatch.setitem(texts.CONSENT_TEXTS, 2, "Новый текст согласия v2.")
    monkeypatch.setenv("CONSENT_VERSION", "2")
    get_settings.cache_clear()
    try:
        await _deliver(session, _callback(user, _button(old_prompt, Action.ACCEPT)), on_callback)

        uid = await _user_id(session, user)
        assert (await session.scalars(select(Consent).where(Consent.user_id == uid))).all() == []
        assert "Новый текст согласия v2." in (await _last(session, user))["text"]
    finally:
        get_settings.cache_clear()


async def test_repeated_accept_is_idempotent(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    await _deliver(session, _bot_started(user, tokens[0]), on_bot_started)
    prompt = await _last(session, user)
    accept = _button(prompt, Action.ACCEPT)

    await _deliver(session, _callback(user, accept), on_callback)
    await _deliver(session, _callback(user, accept), on_callback)  # второе нажатие

    uid = await _user_id(session, user)
    assert len((await session.scalars(select(Consent).where(Consent.user_id == uid))).all()) == 1
    replies = [m["text"] for m in await _outbox(session, user)]
    assert replies.count(texts.ONBOARDED) == 1


# --- удержание не видно воркеру ------------------------------------------------


async def test_held_events_are_invisible_to_worker(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    """Удержание при доме без согласия: awaiting_consent не виден reclaim/claim."""

    await _onboard(session, user, tokens[0])
    await _deliver(session, _message(user, "/revoke"), MESSAGE)
    held = await _deliver(session, _message(user, "течёт"), MESSAGE)
    assert (await _event_status(session, held))[0] == "awaiting_consent"

    await session.execute(
        text(
            "UPDATE inbound_events SET processing_started_at = now() - interval '1 day' "
            "WHERE id = :i"
        ),
        {"i": held},
    )

    from datetime import timedelta

    await inbox.reclaim_stale_processing(
        session, visibility_timeout=timedelta(seconds=1), max_attempts=5
    )
    claimed = await inbox.claim_batch(session, batch_size=100)

    assert held not in {c.id for c in claimed}
    assert (await _event_status(session, held))[0] == "awaiting_consent"


async def test_start_command_for_onboarded_user_says_already_linked(
    session: AsyncSession, tokens: list[str], user: str
) -> None:
    await _onboard(session, user, tokens[0])

    await _deliver(session, _message(user, "/start"), MESSAGE)

    assert (await _last(session, user))["text"] == texts.ALREADY_ONBOARDED.format(address=ADDRESS_1)


async def test_unparseable_callback_is_still_answered(session: AsyncSession, user: str) -> None:
    await _deliver(session, _callback(user, "onb:leave:a:" + "x" * 500), on_callback)

    answers = await session.scalar(
        text(
            "SELECT count(*) FROM outbound_messages WHERE max_chat_id = :c "
            "AND payload::jsonb ? 'callback_id'"
        ),
        {"c": user},
    )
    assert answers == 1
