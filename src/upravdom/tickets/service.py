"""Сервис заявок (TICKET-001): создание, статусы, доступ по номеру."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.models import House, ProblemType, Ticket, TicketEvent, TicketSubscriber
from upravdom.models.enums import (
    JoinReason,
    ResponsibilityZone,
    TicketEventType,
    TicketStatus,
)
from upravdom.tickets.routing import Addressee, resolve_addressee

# Разрешённые переходы. merged — только DEDUP-001.
_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.ACCEPTED: frozenset(
        {
            TicketStatus.IN_PROGRESS,
            TicketStatus.ROUTED_TO_CONTRACTOR,
            TicketStatus.COMPLETED,
        }
    ),
    TicketStatus.NEEDS_DISPATCHER: frozenset({TicketStatus.ACCEPTED, TicketStatus.IN_PROGRESS}),
    TicketStatus.IN_PROGRESS: frozenset(
        {TicketStatus.ROUTED_TO_CONTRACTOR, TicketStatus.COMPLETED}
    ),
    TicketStatus.ROUTED_TO_CONTRACTOR: frozenset(
        {TicketStatus.IN_PROGRESS, TicketStatus.COMPLETED}
    ),
    TicketStatus.COMPLETED: frozenset(),
    TicketStatus.MERGED: frozenset(),
}

_OPEN_STATUSES = frozenset(
    {
        TicketStatus.ACCEPTED,
        TicketStatus.NEEDS_DISPATCHER,
        TicketStatus.IN_PROGRESS,
        TicketStatus.ROUTED_TO_CONTRACTOR,
    }
)


class TicketError(Exception):
    """Базовая ошибка модуля заявок."""


class InvalidStatusTransition(TicketError):
    def __init__(self, from_status: TicketStatus, to_status: TicketStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"запрещённый переход {from_status.value} → {to_status.value}")


class TicketNotFound(TicketError):
    """Заявка не найдена или нет доступа — для вызывающего неотличимо."""


async def create_ticket(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    house_id: uuid.UUID,
    raw_text: str,
    problem_type: str,
    responsibility_zone: ResponsibilityZone,
    confidence: float,
    source_event_id: uuid.UUID | None = None,
    actor: str = "system",
    now: datetime | None = None,
) -> Ticket:
    """Одна транзакция: tickets + created + автор-подписчик. Идемпотентно по source_event_id."""

    now = now or datetime.now(UTC)

    if source_event_id is not None:
        existing = (
            await session.execute(select(Ticket).where(Ticket.source_event_id == source_event_id))
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    house = await session.get(House, house_id)
    pt = await session.get(ProblemType, problem_type)
    if pt is None:
        msg = f"неизвестный problem_type: {problem_type}"
        raise TicketError(msg)

    due_at = now + timedelta(hours=pt.resolution_hours) if pt.resolution_hours is not None else None
    status = (
        TicketStatus.NEEDS_DISPATCHER
        if responsibility_zone is ResponsibilityZone.UNKNOWN
        else TicketStatus.ACCEPTED
    )

    addressee = await resolve_addressee(
        session, house_id, responsibility_zone, problem_type, on=now
    )

    ticket = Ticket(
        id=uuid.uuid4(),
        user_id=user_id,
        house_id=house_id,
        management_company_id=house.management_company_id if house else None,
        raw_text=raw_text,
        problem_type=problem_type,
        responsibility_zone=responsibility_zone,
        confidence=confidence,
        routed_to_org_type=addressee.org_type,
        routed_to_org_id=addressee.org_id,
        status=status,
        due_at=due_at,
        source_event_id=source_event_id,
        created_at=now,
        updated_at=now,
    )
    session.add(ticket)
    await session.flush()
    await session.refresh(ticket, attribute_names=["number"])

    session.add(
        TicketEvent(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            event_type=TicketEventType.CREATED.value,
            from_status=None,
            to_status=status,
            actor=actor,
            payload={
                "problem_type": problem_type,
                "responsibility_zone": responsibility_zone.value,
            },
            created_at=now,
        )
    )
    session.add(
        TicketSubscriber(
            ticket_id=ticket.id,
            user_id=user_id,
            is_author=True,
            joined_at=now,
            join_reason=JoinReason.AUTHOR,
        )
    )

    if addressee.fallback_reason:
        session.add(
            TicketEvent(
                id=uuid.uuid4(),
                ticket_id=ticket.id,
                event_type=TicketEventType.ROUTED.value,
                from_status=None,
                to_status=status,
                actor=actor,
                payload={
                    "reason": addressee.fallback_reason,
                    "org_type": addressee.org_type.value if addressee.org_type else None,
                    "org_id": str(addressee.org_id) if addressee.org_id else None,
                },
                created_at=now,
            )
        )

    await session.flush()
    return ticket


async def change_status(
    session: AsyncSession,
    ticket: Ticket,
    to_status: TicketStatus,
    *,
    actor: str = "dispatcher",
    now: datetime | None = None,
) -> Ticket:
    """Смена статуса + событие status_changed. Запрещённый переход — исключение."""

    now = now or datetime.now(UTC)
    if to_status is TicketStatus.MERGED:
        raise InvalidStatusTransition(ticket.status, to_status)
    allowed = _TRANSITIONS.get(ticket.status, frozenset())
    if to_status not in allowed:
        raise InvalidStatusTransition(ticket.status, to_status)

    from_status = ticket.status
    ticket.status = to_status
    ticket.updated_at = now
    session.add(
        TicketEvent(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            event_type=TicketEventType.STATUS_CHANGED.value,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            payload=None,
            created_at=now,
        )
    )
    await session.flush()
    return ticket


async def get_for_user(session: AsyncSession, number: int, user_id: uuid.UUID) -> Ticket | None:
    """Заявка по номеру только для подписчика. Чужой ≡ несуществующий."""

    ticket = (
        await session.execute(select(Ticket).where(Ticket.number == number))
    ).scalar_one_or_none()
    if ticket is None:
        return None
    sub = await session.get(TicketSubscriber, (ticket.id, user_id))
    if sub is None:
        return None
    return ticket


async def list_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 5
) -> list[Ticket]:
    """Открытые первыми, не больше limit."""

    rows = (
        (
            await session.execute(
                select(Ticket)
                .join(
                    TicketSubscriber,
                    TicketSubscriber.ticket_id == Ticket.id,
                )
                .where(TicketSubscriber.user_id == user_id)
                .order_by(
                    Ticket.status.in_(_OPEN_STATUSES).desc(),
                    Ticket.created_at.desc(),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_by_number(session: AsyncSession, number: int) -> Ticket | None:
    """Служебный доступ без проверки подписчика (CLI)."""

    return (
        await session.execute(select(Ticket).where(Ticket.number == number))
    ).scalar_one_or_none()


def last_addressee_from_ticket(ticket: Ticket) -> Addressee:
    """Срез адресата из полей заявки (без повторного resolve)."""

    return Addressee(
        org_type=ticket.routed_to_org_type,
        org_id=ticket.routed_to_org_id,
        name=None,
        contact=None,
        hours=None,
    )
