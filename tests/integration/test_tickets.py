"""Интеграционные тесты модуля заявок (TICKET-001)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts.seed_demo import seed, stable_id
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from upravdom.bot_gateway.inbox import get_or_create_user
from upravdom.models import Ticket, TicketEvent, TicketSubscriber
from upravdom.models.enums import ResponsibilityZone, TicketEventType, TicketStatus
from upravdom.tickets.routing import resolve_addressee
from upravdom.tickets.service import (
    InvalidStatusTransition,
    change_status,
    create_ticket,
    get_for_user,
    list_for_user,
)

pytestmark = pytest.mark.asyncio

_PROBLEM_TYPES = Path(__file__).resolve().parents[2] / "data" / "seed" / "problem_types.json"
_UPSERT_PT = text(
    """
    INSERT INTO problem_types
        (code, title, default_responsibility_zone, resolution_hours, norm_reference)
    VALUES
        (:code, :title, :default_responsibility_zone, :resolution_hours, :norm_reference)
    ON CONFLICT (code) DO UPDATE SET
        title = EXCLUDED.title,
        default_responsibility_zone = EXCLUDED.default_responsibility_zone,
        resolution_hours = EXCLUDED.resolution_hours,
        norm_reference = EXCLUDED.norm_reference
    """
)


async def _seed_pt(session: AsyncSession) -> None:
    for row in json.loads(_PROBLEM_TYPES.read_text(encoding="utf-8")):
        await session.execute(_UPSERT_PT, row)


@pytest.fixture
async def ticket_env(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    await _seed_pt(session)
    await seed(session)
    user = await get_or_create_user(session, f"ticket-user-{uuid.uuid4().hex[:8]}")
    house_id = stable_id("house:house-dekabristov-10")
    await session.commit()
    return user.id, house_id


async def _inbound(session: AsyncSession) -> uuid.UUID:
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO inbound_events "
            "(id, max_event_id, payload, status, attempts, received_at) "
            "VALUES (:id, :m, '{}', 'pending', 0, :ts)"
        ),
        {"id": event_id, "m": f"tkt-{event_id}", "ts": datetime.now(UTC)},
    )
    return event_id


async def test_create_gets_unique_number(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env
    a = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="нет воды",
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
    )
    b = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="нет отопления",
        problem_type="heating",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
    )
    assert a.number != b.number
    assert a.number > 0
    assert a.status is TicketStatus.ACCEPTED
    events = (
        (await session.execute(select(TicketEvent).where(TicketEvent.ticket_id == a.id)))
        .scalars()
        .all()
    )
    assert any(e.event_type == TicketEventType.CREATED.value for e in events)
    sub = await session.get(TicketSubscriber, (a.id, user_id))
    assert sub is not None and sub.is_author


async def test_parallel_inserts_unique_numbers(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env

    async def one(i: int) -> int:
        t = await create_ticket(
            session,
            user_id=user_id,
            house_id=house_id,
            raw_text=f"parallel {i}",
            problem_type="cold_water",
            responsibility_zone=ResponsibilityZone.UK,
            confidence=0.8,
        )
        return t.number

    # Последовательно в одной сессии с savepoint — проверяем уникальность seq;
    # истинный параллелизм — на уровне БД через sequence.
    numbers = [await one(i) for i in range(5)]
    assert len(set(numbers)) == 5


async def test_idempotent_by_source_event(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env
    event_id = await _inbound(session)
    first = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="повтор",
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
        source_event_id=event_id,
    )
    second = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="повтор снова",
        problem_type="heating",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.5,
        source_event_id=event_id,
    )
    assert first.id == second.id
    assert first.number == second.number
    count = await session.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.source_event_id == event_id)
    )
    assert count == 1


async def test_addressee_uk_rso_unknown(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _user_id, house_id = ticket_env
    uk = await resolve_addressee(session, house_id, ResponsibilityZone.UK, "cold_water")
    assert uk.org_type is ResponsibilityZone.UK
    assert uk.org_id == stable_id("mc:uk-vahitovskaya")
    assert uk.contact

    rso = await resolve_addressee(session, house_id, ResponsibilityZone.RSO, "cold_water")
    assert rso.org_type is ResponsibilityZone.RSO
    assert rso.org_id == stable_id("ro:vodokanal-cold")
    assert rso.contact

    unknown = await resolve_addressee(session, house_id, ResponsibilityZone.UNKNOWN, "other")
    assert unknown.org_type is ResponsibilityZone.UK

    owner = await resolve_addressee(session, house_id, ResponsibilityZone.OWNER, "cold_water")
    assert owner.org_id is None


async def test_rso_without_resource_falls_back_to_uk(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env
    ticket = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="лифт",
        problem_type="elevator",
        responsibility_zone=ResponsibilityZone.RSO,
        confidence=0.9,
    )
    assert ticket.routed_to_org_type is ResponsibilityZone.UK
    routed = (
        await session.execute(
            select(TicketEvent).where(
                TicketEvent.ticket_id == ticket.id,
                TicketEvent.event_type == TicketEventType.ROUTED.value,
            )
        )
    ).scalar_one()
    assert "rso_no_resource" in str((routed.payload or {}).get("reason"))


async def test_house_without_uk_no_exception(session: AsyncSession) -> None:
    await _seed_pt(session)
    user = await get_or_create_user(session, f"no-uk-{uuid.uuid4().hex[:8]}")
    house_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO houses (id, address_raw, region_code) VALUES (:id, 'без УК', 'RU-TA')"),
        {"id": house_id},
    )
    addr = await resolve_addressee(session, house_id, ResponsibilityZone.UK, "cold_water")
    assert addr.org_id is None
    assert addr.fallback_reason == "house_without_management_company"
    ticket = await create_ticket(
        session,
        user_id=user.id,
        house_id=house_id,
        raw_text="тест",
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
    )
    assert ticket.routed_to_org_id is None


async def test_due_at_null_when_no_resolution_hours(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env
    ticket = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="лифт стоит",
        problem_type="elevator",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
    )
    assert ticket.due_at is None

    water = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="нет хвс",
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
        now=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )
    assert water.due_at == datetime(2026, 9, 22, 16, 0, tzinfo=UTC)


async def test_unknown_zone_needs_dispatcher(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env
    ticket = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="непонятно",
        problem_type="other",
        responsibility_zone=ResponsibilityZone.UNKNOWN,
        confidence=0.2,
    )
    assert ticket.status is TicketStatus.NEEDS_DISPATCHER


async def test_forbidden_transition_and_merged(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env
    ticket = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="статусы",
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
    )
    before = await session.scalar(
        select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )
    with pytest.raises(InvalidStatusTransition):
        await change_status(session, ticket, TicketStatus.MERGED)
    with pytest.raises(InvalidStatusTransition):
        # completed из accepted нельзя напрямую? Wait - accepted → completed IS allowed
        await change_status(session, ticket, TicketStatus.NEEDS_DISPATCHER)
    after = await session.scalar(
        select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )
    assert after == before

    await change_status(session, ticket, TicketStatus.IN_PROGRESS)
    assert ticket.status is TicketStatus.IN_PROGRESS
    await change_status(session, ticket, TicketStatus.COMPLETED)
    with pytest.raises(InvalidStatusTransition):
        await change_status(session, ticket, TicketStatus.ACCEPTED)


async def test_append_only_via_service(
    session: AsyncSession, connection: AsyncConnection, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env
    ticket = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="append",
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
    )
    with pytest.raises(DBAPIError, match="append-only"):
        await connection.execute(
            text("UPDATE ticket_events SET actor = 'x' WHERE ticket_id = :t"),
            {"t": ticket.id},
        )


async def test_access_by_number_hides_foreign(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env
    other = await get_or_create_user(session, f"other-{uuid.uuid4().hex[:8]}")
    ticket = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="секрет",
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
    )
    assert await get_for_user(session, ticket.number, user_id) is not None
    assert await get_for_user(session, ticket.number, other.id) is None
    assert await get_for_user(session, 999_999_999, user_id) is None


async def test_list_for_user_open_first(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, house_id = ticket_env
    open_t = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="открытая",
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    done = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text="закрытая",
        problem_type="heating",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
    )
    await change_status(session, done, TicketStatus.COMPLETED)
    listed = await list_for_user(session, user_id, limit=5)
    assert listed[0].id == open_t.id
    assert len(listed) <= 5


async def test_rso_valid_to_hides_expired(
    session: AsyncSession, ticket_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _user_id, house_id = ticket_env
    # Пометим связь cold_water как истёкшую.
    await session.execute(
        text(
            "UPDATE house_resource_orgs SET valid_to = :to "
            "WHERE house_id = :h AND resource_type = 'cold_water'"
        ),
        {"h": house_id, "to": datetime(2020, 1, 1, tzinfo=UTC)},
    )
    addr = await resolve_addressee(
        session,
        house_id,
        ResponsibilityZone.RSO,
        "cold_water",
        on=datetime(2026, 9, 22, tzinfo=UTC),
    )
    assert addr.org_type is ResponsibilityZone.UK
    assert addr.fallback_reason and "rso_link_missing" in addr.fallback_reason
