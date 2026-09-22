"""Сценарии 1–11 DEDUP-001 на реальном Postgres и настоящих эмбеддингах.

Похожесть намеренно не подменяется фейком: порог склейки — продуктовое
решение, и тест должен ломаться, если модель или порог поедут
(architecture.md §6.5).
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from scripts.seed_demo import seed, stable_id
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import inbox
from upravdom.bot_gateway.dispatcher import route_callback
from upravdom.bot_gateway.inbox import ClaimedEvent, get_or_create_user
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.classifier.llm.fake import FakeLlmClient
from upravdom.classifier.schema import LlmClassification
from upravdom.config import get_settings
from upravdom.dedup.callbacks import encode_split
from upravdom.flow.handlers import on_message
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.models import Ticket, TicketEvent, TicketSubscriber
from upravdom.models.enums import ResponsibilityZone, TicketEventType, TicketStatus
from upravdom.onboarding import service as onboarding_service
from upravdom.tickets.service import change_status, list_for_user

pytestmark = pytest.mark.asyncio

_ts = itertools.count(1_758_580_000_000, 1000)
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

HOUSE_A = "house:house-dekabristov-10"
HOUSE_B = "house:house-lumumby-5"


async def _seed_pt(session: AsyncSession) -> None:
    for row in json.loads(_PROBLEM_TYPES.read_text(encoding="utf-8")):
        await session.execute(_UPSERT_PT, row)


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


def _llm(problem_type: str = "hot_water") -> LlmClassification:
    return LlmClassification(
        problem_type=problem_type,
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.95,
        cited_fragments=[1],
        reasoning="тест",
        clarifying_question=None,
        clarifying_options=[],
    )


async def _fixed_search(*_a: object, **_k: object) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=uuid.uuid4(),
            ref="п. 5 абз. 1",
            text="ПП РФ №491, п. 5 абз. 1\nсети до первого отключающего устройства.",
            score=0.9,
            source_key="pp491",
        )
    ]


@pytest.fixture
def patch_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("upravdom.classifier.service.search", _fixed_search)


@pytest.fixture
async def houses(session: AsyncSession) -> None:
    await _seed_pt(session)
    await seed(session)
    await session.commit()


async def _resident(session: AsyncSession, house_key: str) -> str:
    max_user_id = f"ddp-{uuid.uuid4().hex[:10]}"
    user = await get_or_create_user(session, max_user_id)
    await onboarding_service.bind_house(session, user.id, stable_id(house_key))
    await onboarding_service.grant_consent(
        session, user.id, version=get_settings().consent_version, source="test"
    )
    await session.flush()
    return max_user_id


async def _say(
    session: AsyncSession, user: str, body: str, *, problem_type: str = "hot_water"
) -> None:
    payload = _message(user, body)
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
    llm = FakeLlmClient(responses=[_llm(problem_type)])
    await on_message(session, ClaimedEvent(event_id, parsed.event_id, payload, 1), llm=llm)
    await inbox.mark_done(session, event_id)


async def _press(session: AsyncSession, user: str, payload_str: str) -> None:
    payload = _callback(user, payload_str)
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
    # Через диспетчер — заодно проверяем маршрут по префиксу `ddp:`.
    await route_callback(session, ClaimedEvent(event_id, parsed.event_id, payload, 1))
    await inbox.mark_done(session, event_id)


async def _replies(session: AsyncSession, chat: str) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT payload FROM outbound_messages WHERE max_chat_id = :c "
            "AND NOT (payload::jsonb ? 'callback_id') ORDER BY created_at, id"
        ),
        {"c": chat},
    )
    return [row[0]["text"] for row in rows]


async def _tickets(session: AsyncSession, user_max_id: str) -> list[Ticket]:
    user = await get_or_create_user(session, user_max_id)
    return (
        (
            await session.execute(
                select(Ticket).where(Ticket.user_id == user.id).order_by(Ticket.created_at)
            )
        )
        .scalars()
        .all()  # type: ignore[return-value]
    )


# --- 1 -----------------------------------------------------------------------


async def test_neighbour_is_merged_and_subscribed(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)

    await _say(session, author, "нет горячей воды")
    await _say(session, neighbour, "горячей воды нет")

    head = (await _tickets(session, author))[0]
    merged = (await _tickets(session, neighbour))[0]
    assert head.status is TicketStatus.ACCEPTED
    assert merged.status is TicketStatus.MERGED
    assert merged.duplicate_of_ticket_id == head.id
    assert merged.dedup_group_id == head.id == head.dedup_group_id

    neighbour_user = await get_or_create_user(session, neighbour)
    subscription = await session.get(TicketSubscriber, (head.id, neighbour_user.id))
    assert subscription is not None and not subscription.is_author

    reply = (await _replies(session, neighbour))[-1]
    assert f"заявка № {head.number}" in reply.lower()
    assert "подписал вас" in reply

    joined = (
        await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == head.id,
                TicketEvent.event_type == TicketEventType.SUBSCRIBER_JOINED.value,
            )
        )
    ).scalar_one()
    assert joined.payload == {"merged_ticket_number": merged.number, "subscribers_count": 2}


# --- 2, 3, 4, 5, 6 ------------------------------------------------------------


async def test_other_house_is_not_merged(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    author = await _resident(session, HOUSE_A)
    stranger = await _resident(session, HOUSE_B)

    await _say(session, author, "нет горячей воды")
    await _say(session, stranger, "горячей воды нет")

    assert (await _tickets(session, stranger))[0].status is TicketStatus.ACCEPTED


async def test_other_problem_type_is_not_merged(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)

    await _say(session, author, "нет горячей воды", problem_type="hot_water")
    await _say(session, neighbour, "горячей воды нет", problem_type="heating")

    assert (await _tickets(session, neighbour))[0].status is TicketStatus.ACCEPTED


async def test_head_outside_window_is_not_merged(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)

    await _say(session, author, "нет горячей воды")
    head = (await _tickets(session, author))[0]
    old = datetime.now(UTC) - timedelta(hours=get_settings().dedup_window_hours + 1)
    await session.execute(
        text("UPDATE tickets SET created_at = :t WHERE id = :i"), {"t": old, "i": head.id}
    )

    await _say(session, neighbour, "горячей воды нет")

    assert (await _tickets(session, neighbour))[0].status is TicketStatus.ACCEPTED


async def test_completed_head_is_not_merged(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)

    await _say(session, author, "нет горячей воды")
    head = (await _tickets(session, author))[0]
    await change_status(session, head, TicketStatus.COMPLETED)

    await _say(session, neighbour, "горячей воды нет")

    assert (await _tickets(session, neighbour))[0].status is TicketStatus.ACCEPTED


async def test_problem_type_other_is_never_merged(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    """Для `other` совпадение типа ничего не говорит об одной аварии."""

    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)

    await _say(session, author, "нет горячей воды", problem_type="other")
    await _say(session, neighbour, "горячей воды нет", problem_type="other")

    assert (await _tickets(session, neighbour))[0].status is TicketStatus.ACCEPTED


# --- 7, 8, 9 ------------------------------------------------------------------


async def test_split_creates_own_ticket_and_unsubscribes(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)
    await _say(session, author, "нет горячей воды")
    await _say(session, neighbour, "горячей воды нет")
    head = (await _tickets(session, author))[0]
    merged = (await _tickets(session, neighbour))[0]

    await _press(session, neighbour, encode_split(merged.number))

    await session.refresh(merged)
    assert merged.status is TicketStatus.ACCEPTED
    assert merged.duplicate_of_ticket_id is None
    assert merged.routed_to_org_id is not None
    assert merged.due_at is not None

    neighbour_user = await get_or_create_user(session, neighbour)
    assert await session.get(TicketSubscriber, (head.id, neighbour_user.id)) is None

    events = {
        (e.ticket_id, e.event_type)
        for e in (await session.execute(select(TicketEvent))).scalars().all()
    }
    assert (merged.id, TicketEventType.STATUS_CHANGED.value) in events
    assert (head.id, TicketEventType.SUBSCRIBER_LEFT.value) in events
    assert "принята отдельно" in (await _replies(session, neighbour))[-1]


async def test_split_of_foreign_ticket_changes_nothing(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)
    stranger = await _resident(session, HOUSE_A)
    await _say(session, author, "нет горячей воды")
    await _say(session, neighbour, "горячей воды нет")
    merged = (await _tickets(session, neighbour))[0]
    replies_before = len(await _replies(session, stranger))

    await _press(session, stranger, encode_split(merged.number))

    await session.refresh(merged)
    assert merged.status is TicketStatus.MERGED
    assert merged.duplicate_of_ticket_id is not None
    assert len(await _replies(session, stranger)) == replies_before


async def test_second_split_press_is_idempotent(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)
    await _say(session, author, "нет горячей воды")
    await _say(session, neighbour, "горячей воды нет")
    merged = (await _tickets(session, neighbour))[0]
    head = (await _tickets(session, author))[0]

    await _press(session, neighbour, encode_split(merged.number))
    replies_after_first = await _replies(session, neighbour)
    await _press(session, neighbour, encode_split(merged.number))

    assert await _replies(session, neighbour) == replies_after_first
    left = await session.scalar(
        select(func.count())
        .select_from(TicketEvent)
        .where(
            TicketEvent.ticket_id == head.id,
            TicketEvent.event_type == TicketEventType.SUBSCRIBER_LEFT.value,
        )
    )
    assert left == 1


# --- 10 -----------------------------------------------------------------------


async def test_embedding_failure_does_not_break_the_flow(
    session: AsyncSession,
    houses: None,
    patch_search: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)
    await _say(session, author, "нет горячей воды")

    async def no_vector(_text: str) -> list[float] | None:
        return None

    monkeypatch.setattr("upravdom.flow.handlers.embed_message", no_vector)
    await _say(session, neighbour, "горячей воды нет")

    ticket = (await _tickets(session, neighbour))[0]
    assert ticket.status is TicketStatus.ACCEPTED
    assert ticket.text_embedding is None
    assert "принята" in (await _replies(session, neighbour))[-1]


# --- 11 -----------------------------------------------------------------------


async def test_same_resident_writing_twice_is_not_subscribed_twice(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    author = await _resident(session, HOUSE_A)
    await _say(session, author, "нет горячей воды")
    await _say(session, author, "горячей воды нет")

    user = await get_or_create_user(session, author)
    head, merged = await _tickets(session, author)
    assert merged.status is TicketStatus.MERGED

    subscription = await session.get(TicketSubscriber, (head.id, user.id))
    assert subscription is not None and subscription.is_author
    joined = await session.scalar(
        select(func.count())
        .select_from(TicketEvent)
        .where(
            TicketEvent.ticket_id == head.id,
            TicketEvent.event_type == TicketEventType.SUBSCRIBER_JOINED.value,
        )
    )
    assert joined == 0

    # Дубль не показывается в выдаче статуса рядом с головной заявкой.
    listed = await list_for_user(session, user.id, limit=5)
    assert [t.id for t in listed] == [head.id]
    assert "уже есть заявка" in (await _replies(session, author))[-1]


# --- идемпотентность воркера --------------------------------------------------


async def test_retry_of_merged_event_does_not_create_second_merge(
    session: AsyncSession, houses: None, patch_search: None
) -> None:
    """Ретрай того же события: ни второй склейки, ни второй подписки."""

    author = await _resident(session, HOUSE_A)
    neighbour = await _resident(session, HOUSE_A)
    await _say(session, author, "нет горячей воды")

    payload = _message(neighbour, "горячей воды нет")
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
    for attempt in (1, 2):
        llm = FakeLlmClient(responses=[_llm()])
        await on_message(
            session, ClaimedEvent(event_id, parsed.event_id, payload, attempt), llm=llm
        )

    head = (await _tickets(session, author))[0]
    assert len(await _tickets(session, neighbour)) == 1
    subscribers = await session.scalar(
        select(func.count())
        .select_from(TicketSubscriber)
        .where(TicketSubscriber.ticket_id == head.id)
    )
    assert subscribers == 2
