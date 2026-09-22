"""Сценарии 1–8 STATUS-001 на реальном Postgres.

Отправка в MAX проверяется по очереди `outbound_messages`: уведомление
подписчику адресуется по `user_id` (max_api.md §7), а не по `chat_id`, —
`chat_id` диалога с ботом в `users` не хранится и не равен `user_id`.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from scripts.seed_demo import seed, stable_id
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import inbox
from upravdom.bot_gateway.inbox import ClaimedEvent, get_or_create_user
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.config import get_settings
from upravdom.flow.handlers import on_message
from upravdom.models import TicketEvent, TicketSubscriber
from upravdom.models.enums import (
    JoinReason,
    ResponsibilityZone,
    TicketEventType,
    TicketStatus,
)
from upravdom.onboarding import service as onboarding_service
from upravdom.tickets import texts as ticket_texts
from upravdom.tickets.service import (
    InvalidStatusTransition,
    TicketClosed,
    change_status,
    create_ticket,
    reroute,
)

pytestmark = pytest.mark.asyncio

_ts = itertools.count(1_758_570_000_000, 1000)
_PROBLEM_TYPES = Path(__file__).resolve().parents[2] / "data" / "seed" / "problem_types.json"
_UPSERT_PT = text(
    """
    INSERT INTO problem_types
        (code, title, default_responsibility_zone, resolution_hours, norm_reference)
    VALUES
        (:code, :title, :default_responsibility_zone, :resolution_hours, :norm_reference)
    ON CONFLICT (code) DO NOTHING
    """
)


async def _seed_pt(session: AsyncSession) -> None:
    for row in json.loads(_PROBLEM_TYPES.read_text(encoding="utf-8")):
        await session.execute(_UPSERT_PT, row)


async def _resident(session: AsyncSession, house_id: uuid.UUID, *, consent: bool = True) -> Any:
    """Житель с привязанным домом и (по умолчанию) действующим согласием."""

    user = await get_or_create_user(session, f"st-{uuid.uuid4().hex[:10]}")
    await onboarding_service.bind_house(session, user.id, house_id)
    if consent:
        await onboarding_service.grant_consent(
            session, user.id, version=get_settings().consent_version, source="test"
        )
    return user


@pytest.fixture
async def env(session: AsyncSession) -> tuple[uuid.UUID, Any]:
    await _seed_pt(session)
    await seed(session)
    house_id = stable_id("house:house-dekabristov-10")
    author = await _resident(session, house_id)
    await session.commit()
    return house_id, author


async def _ticket(session: AsyncSession, house_id: uuid.UUID, author: Any) -> Any:
    return await create_ticket(
        session,
        user_id=author.id,
        house_id=house_id,
        raw_text="нет холодной воды",
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.95,
    )


async def _subscribe(session: AsyncSession, ticket_id: uuid.UUID, user_id: uuid.UUID) -> None:
    session.add(
        TicketSubscriber(
            ticket_id=ticket_id,
            user_id=user_id,
            is_author=False,
            joined_at=datetime.now(UTC),
            join_reason=JoinReason.MANUAL,
        )
    )
    await session.flush()


async def _notices(session: AsyncSession, max_user_id: str) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT payload FROM outbound_messages "
            "WHERE payload::jsonb ->> 'user_id' = :u ORDER BY created_at, id"
        ),
        {"u": max_user_id},
    )
    return [row[0]["text"] for row in rows]


# --- 1 -----------------------------------------------------------------------


async def test_status_change_notifies_author(
    session: AsyncSession, env: tuple[uuid.UUID, Any]
) -> None:
    house_id, author = env
    ticket = await _ticket(session, house_id, author)

    await change_status(session, ticket, TicketStatus.IN_PROGRESS)

    notices = await _notices(session, author.max_user_id)
    assert len(notices) == 1
    assert f"Заявка № {ticket.number}" in notices[0]
    assert "статус изменён — в работе" in notices[0]


async def test_notification_is_addressed_by_user_id(
    session: AsyncSession, env: tuple[uuid.UUID, Any]
) -> None:
    """Адресуется житель, а не чат: `chat_id` диалога у нас не сохранён."""

    house_id, author = env
    ticket = await _ticket(session, house_id, author)
    await change_status(session, ticket, TicketStatus.IN_PROGRESS)

    row = (
        await session.execute(
            text(
                "SELECT max_chat_id, payload FROM outbound_messages "
                "WHERE payload::jsonb ->> 'user_id' = :u"
            ),
            {"u": author.max_user_id},
        )
    ).one()
    assert row.payload["user_id"] == author.max_user_id
    assert row.max_chat_id == author.max_user_id
    buttons = row.payload["attachments"][0]["payload"]["buttons"]
    assert buttons[0][0]["payload"] == f"clf:st:{ticket.number}"


# --- 2 -----------------------------------------------------------------------


async def test_all_subscribers_notified(session: AsyncSession, env: tuple[uuid.UUID, Any]) -> None:
    house_id, author = env
    ticket = await _ticket(session, house_id, author)
    second = await _resident(session, house_id)
    third = await _resident(session, house_id)
    await _subscribe(session, ticket.id, second.id)
    await _subscribe(session, ticket.id, third.id)

    await change_status(session, ticket, TicketStatus.IN_PROGRESS)

    for user in (author, second, third):
        assert len(await _notices(session, user.max_user_id)) == 1


# --- 3 -----------------------------------------------------------------------


async def test_revoked_consent_gets_no_notification(
    session: AsyncSession, env: tuple[uuid.UUID, Any]
) -> None:
    house_id, author = env
    ticket = await _ticket(session, house_id, author)
    silent = await _resident(session, house_id)
    never = await _resident(session, house_id, consent=False)
    await _subscribe(session, ticket.id, silent.id)
    await _subscribe(session, ticket.id, never.id)
    await onboarding_service.revoke_consent(session, silent.id)

    await change_status(session, ticket, TicketStatus.IN_PROGRESS)

    assert len(await _notices(session, author.max_user_id)) == 1
    assert await _notices(session, silent.max_user_id) == []
    assert await _notices(session, never.max_user_id) == []


# --- 4 -----------------------------------------------------------------------


async def test_reroute_to_rso_writes_event_and_notifies(
    session: AsyncSession, env: tuple[uuid.UUID, Any]
) -> None:
    house_id, author = env
    ticket = await _ticket(session, house_id, author)
    status_before = ticket.status

    await reroute(session, ticket, zone=ResponsibilityZone.RSO, reason="не наша сеть")

    assert ticket.routed_to_org_type is ResponsibilityZone.RSO
    assert ticket.routed_to_org_id == stable_id("ro:vodokanal-cold")
    assert ticket.status is status_before  # переадресация и статус — разные оси

    event = (
        await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == ticket.id,
                TicketEvent.event_type == TicketEventType.ROUTED.value,
            )
        )
    ).scalar_one()
    assert isinstance(event.payload, dict)
    target = event.payload["to"]
    assert isinstance(target, dict)
    assert target["org_type"] == "rso"
    assert event.payload["reason"] == "не наша сеть"

    notice = (await _notices(session, author.max_user_id))[-1]
    assert f"Заявка № {ticket.number} передана" in notice
    assert "Водоканал" in notice


async def test_completed_ticket_is_not_rerouted(
    session: AsyncSession, env: tuple[uuid.UUID, Any]
) -> None:
    house_id, author = env
    ticket = await _ticket(session, house_id, author)
    await change_status(session, ticket, TicketStatus.COMPLETED)
    events_before = await session.scalar(
        select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )

    with pytest.raises(TicketClosed):
        await reroute(session, ticket, zone=ResponsibilityZone.RSO)

    events_after = await session.scalar(
        select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )
    assert events_after == events_before


# --- 5 -----------------------------------------------------------------------


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


async def _deliver(session: AsyncSession, payload: dict[str, Any]) -> None:
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
    await on_message(session, ClaimedEvent(event_id, parsed.event_id, payload, 1))
    await inbox.mark_done(session, event_id)


async def _chat_texts(session: AsyncSession, chat: str) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT payload FROM outbound_messages WHERE max_chat_id = :c "
            "AND NOT (payload::jsonb ? 'callback_id') "
            "AND NOT (payload::jsonb ? 'user_id') ORDER BY created_at, id"
        ),
        {"c": chat},
    )
    return [row[0]["text"] for row in rows]


async def test_status_by_number_shows_history(
    session: AsyncSession, env: tuple[uuid.UUID, Any]
) -> None:
    house_id, author = env
    ticket = await _ticket(session, house_id, author)
    joined = await _resident(session, house_id)
    await _subscribe(session, ticket.id, joined.id)
    await change_status(session, ticket, TicketStatus.IN_PROGRESS)
    await reroute(session, ticket, zone=ResponsibilityZone.RSO)
    await change_status(session, ticket, TicketStatus.COMPLETED)

    await _deliver(session, _message(author.max_user_id, f"статус {ticket.number}"))

    reply = (await _chat_texts(session, author.max_user_id))[-1]
    assert ticket_texts.HISTORY_HEADER in reply
    assert "принята" in reply
    assert "в работе" in reply
    assert "передана: " in reply
    assert "выполнена" in reply
    # Присоединившиеся — числом, без имён и идентификаторов (architecture.md §11).
    assert ticket_texts.joined_line(1) in reply
    assert joined.max_user_id not in reply
    assert str(joined.id) not in reply


async def test_history_hides_service_routing_from_create(
    session: AsyncSession, env: tuple[uuid.UUID, Any]
) -> None:
    """`routed` из create_ticket (РСО по ресурсу не нашлась) — не передача заявки."""

    house_id, author = env
    ticket = await create_ticket(
        session,
        user_id=author.id,
        house_id=house_id,
        raw_text="лифт застрял",
        problem_type="elevator",
        responsibility_zone=ResponsibilityZone.RSO,
        confidence=0.9,
    )

    await _deliver(session, _message(author.max_user_id, f"статус {ticket.number}"))

    reply = (await _chat_texts(session, author.max_user_id))[-1]
    assert "передана: " not in reply
    assert "принята" in reply


# --- 6 -----------------------------------------------------------------------


async def test_forbidden_transition_writes_nothing(
    session: AsyncSession, env: tuple[uuid.UUID, Any]
) -> None:
    house_id, author = env
    ticket = await _ticket(session, house_id, author)

    with pytest.raises(InvalidStatusTransition):
        await change_status(session, ticket, TicketStatus.NEEDS_DISPATCHER)

    assert ticket.status is TicketStatus.ACCEPTED
    assert await _notices(session, author.max_user_id) == []
    events = await session.scalar(
        select(func.count())
        .select_from(TicketEvent)
        .where(
            TicketEvent.ticket_id == ticket.id,
            TicketEvent.event_type == TicketEventType.STATUS_CHANGED.value,
        )
    )
    assert events == 0


# --- 7 -----------------------------------------------------------------------


async def test_failure_inside_change_status_rolls_back_both(
    session: AsyncSession,
    env: tuple[uuid.UUID, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Статус и уведомление — одна транзакция: не должно остаться ни того, ни другого."""

    house_id, author = env
    ticket = await _ticket(session, house_id, author)
    ticket_id = ticket.id
    await session.commit()

    async def boom(*_a: object, **_k: object) -> int:
        msg = "сбой рассылки"
        raise RuntimeError(msg)

    monkeypatch.setattr("upravdom.tickets.service.notify_subscribers", boom)

    with pytest.raises(RuntimeError):
        await change_status(session, ticket, TicketStatus.IN_PROGRESS)
    await session.rollback()

    stored = await session.scalar(
        text("SELECT status FROM tickets WHERE id = :i"), {"i": ticket_id}
    )
    assert stored == TicketStatus.ACCEPTED.value
    assert await _notices(session, author.max_user_id) == []
    events = await session.scalar(
        select(func.count())
        .select_from(TicketEvent)
        .where(
            TicketEvent.ticket_id == ticket_id,
            TicketEvent.event_type == TicketEventType.STATUS_CHANGED.value,
        )
    )
    assert events == 0


# --- 8 -----------------------------------------------------------------------


async def test_completed_notice_invites_to_write_again(
    session: AsyncSession, env: tuple[uuid.UUID, Any]
) -> None:
    house_id, author = env
    ticket = await _ticket(session, house_id, author)

    await change_status(session, ticket, TicketStatus.COMPLETED)

    notice = (await _notices(session, author.max_user_id))[-1]
    assert notice == ticket_texts.NOTIFY_COMPLETED.format(number=f"№ {ticket.number}")
    assert "создам новую заявку" in notice
