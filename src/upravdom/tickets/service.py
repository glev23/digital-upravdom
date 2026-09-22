"""Сервис заявок (TICKET-001): создание, статусы, доступ по номеру."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.config import Settings
from upravdom.models import House, ProblemType, Ticket, TicketEvent, TicketSubscriber
from upravdom.models.enums import (
    JoinReason,
    ResponsibilityZone,
    TicketEventType,
    TicketStatus,
)
from upravdom.tickets.notify import notify_subscribers
from upravdom.tickets.routing import Addressee, org_display, resolve_addressee
from upravdom.tickets.texts import rerouted_notice, status_changed_notice

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

# Переадресовывать нечего: заявка либо закрыта, либо это склейка (DEDUP-001).
_CLOSED_STATUSES = frozenset({TicketStatus.COMPLETED, TicketStatus.MERGED})


class TicketError(Exception):
    """Базовая ошибка модуля заявок."""


class InvalidStatusTransition(TicketError):
    def __init__(self, from_status: TicketStatus, to_status: TicketStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"запрещённый переход {from_status.value} → {to_status.value}")


class TicketNotFound(TicketError):
    """Заявка не найдена или нет доступа — для вызывающего неотличимо."""


class TicketClosed(TicketError):
    """Закрытую или склеенную заявку переадресовывать некуда."""

    def __init__(self, status: TicketStatus) -> None:
        self.status = status
        super().__init__(f"заявка в статусе {status.value} не переадресуется")


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
    settings: Settings | None = None,
) -> Ticket:
    """Смена статуса + событие status_changed + уведомление подписчиков.

    Всё тремя шагами в одной транзакции (STATUS-001): статус без уведомления
    или уведомление без статуса — рассинхрон, который житель увидит. Коммитит
    вызывающий, как и весь остальной код обработчиков (architecture.md §3).
    Запрещённый переход — исключение до любой записи: ни события, ни
    уведомления.
    """

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
    await notify_subscribers(
        session,
        ticket,
        status_changed_notice(ticket.number, to_status),
        settings=settings,
    )
    await session.flush()
    return ticket


async def reroute(
    session: AsyncSession,
    ticket: Ticket,
    *,
    zone: ResponsibilityZone,
    org_id: uuid.UUID | None = None,
    actor: str = "dispatcher",
    reason: str | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> Ticket:
    """Передача заявки другой организации: событие `routed` + уведомление.

    Переадресация и статус — разные оси: «передано подрядчику» это статус
    `routed_to_contractor`, а «передано в РСО» — смена адресата, поэтому
    статус здесь не меняется (STATUS-001).
    """

    now = now or datetime.now(UTC)
    if ticket.status in _CLOSED_STATUSES:
        raise TicketClosed(ticket.status)
    if zone in (ResponsibilityZone.OWNER, ResponsibilityZone.MUNICIPALITY):
        msg = f"для зоны {zone.value} адресата не существует — переадресация невозможна"
        raise TicketError(msg)

    if org_id is not None:
        new_type: ResponsibilityZone | None = zone
        new_id: uuid.UUID | None = org_id
        name, contact = await org_display(session, new_type, new_id)
    else:
        addressee = await resolve_addressee(
            session, ticket.house_id, zone, ticket.problem_type, on=now
        )
        new_type, new_id = addressee.org_type, addressee.org_id
        name, contact = addressee.name, addressee.contact

    previous = {
        "org_type": ticket.routed_to_org_type.value if ticket.routed_to_org_type else None,
        "org_id": str(ticket.routed_to_org_id) if ticket.routed_to_org_id else None,
    }
    ticket.routed_to_org_type = new_type
    ticket.routed_to_org_id = new_id
    if zone is not ticket.responsibility_zone:
        ticket.responsibility_zone = zone
    ticket.updated_at = now

    session.add(
        TicketEvent(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            event_type=TicketEventType.ROUTED.value,
            from_status=ticket.status,
            to_status=ticket.status,
            actor=actor,
            payload={
                "from": previous,
                "to": {
                    "org_type": new_type.value if new_type else None,
                    "org_id": str(new_id) if new_id else None,
                    "name": name,
                },
                "reason": reason,
            },
            created_at=now,
        )
    )
    await notify_subscribers(
        session,
        ticket,
        rerouted_notice(ticket.number, name, contact),
        settings=settings,
    )
    await session.flush()
    return ticket


async def create_merged_ticket(
    session: AsyncSession,
    *,
    head: Ticket,
    user_id: uuid.UUID,
    house_id: uuid.UUID,
    raw_text: str,
    problem_type: str,
    responsibility_zone: ResponsibilityZone,
    confidence: float,
    text_embedding: list[float] | None = None,
    source_event_id: uuid.UUID | None = None,
    actor: str = "system",
    now: datetime | None = None,
) -> Ticket:
    """Заявка-дубль сразу в статусе `merged` (DEDUP-001).

    Отдельная функция, а не `create_ticket` + `change_status`: таблица
    переходов намеренно запрещает переход в `merged`. Обращение жителя не
    исчезает — остаётся строкой со ссылкой на головную (architecture.md §5.2).
    """

    now = now or datetime.now(UTC)

    if source_event_id is not None:
        existing = await get_by_source_event(session, source_event_id)
        if existing is not None:
            return existing

    house = await session.get(House, house_id)
    pt = await session.get(ProblemType, problem_type)
    if pt is None:
        msg = f"неизвестный problem_type: {problem_type}"
        raise TicketError(msg)

    if head.dedup_group_id is None:
        head.dedup_group_id = head.id

    ticket = Ticket(
        id=uuid.uuid4(),
        user_id=user_id,
        house_id=house_id,
        management_company_id=house.management_company_id if house else None,
        raw_text=raw_text,
        problem_type=problem_type,
        responsibility_zone=responsibility_zone,
        confidence=confidence,
        routed_to_org_type=None,
        routed_to_org_id=None,
        status=TicketStatus.MERGED,
        due_at=None,
        text_embedding=text_embedding,
        source_event_id=source_event_id,
        dedup_group_id=head.dedup_group_id,
        duplicate_of_ticket_id=head.id,
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
            to_status=TicketStatus.MERGED,
            actor=actor,
            payload={
                "problem_type": problem_type,
                "responsibility_zone": responsibility_zone.value,
                "duplicate_of_ticket_number": head.number,
            },
            created_at=now,
        )
    )
    # Автор остаётся автором своего обращения (инвариант TICKET-001); из выдачи
    # статуса merged-строка убрана в `list_for_user`, чтобы житель не видел
    # свой дубль рядом с головной заявкой.
    session.add(
        TicketSubscriber(
            ticket_id=ticket.id,
            user_id=user_id,
            is_author=True,
            joined_at=now,
            join_reason=JoinReason.AUTHOR,
        )
    )
    await session.flush()
    return ticket


async def split_merged(
    session: AsyncSession,
    merged: Ticket,
    *,
    actor: str = "resident",
    now: datetime | None = None,
) -> Ticket:
    """Выход из склейки: merged → обычная заявка со своим адресатом и сроком.

    Единственный разрешённый переход из `merged` — общая таблица переходов
    его по-прежнему запрещает (DEDUP-001). Ложная склейка дороже пропущенного
    дубля, поэтому выход обязателен (architecture.md §6.5).
    """

    now = now or datetime.now(UTC)
    if merged.status is not TicketStatus.MERGED:
        raise InvalidStatusTransition(merged.status, TicketStatus.ACCEPTED)

    head_id = merged.duplicate_of_ticket_id
    pt = await session.get(ProblemType, merged.problem_type)
    addressee = await resolve_addressee(
        session, merged.house_id, merged.responsibility_zone, merged.problem_type, on=now
    )
    to_status = (
        TicketStatus.NEEDS_DISPATCHER
        if merged.responsibility_zone is ResponsibilityZone.UNKNOWN
        else TicketStatus.ACCEPTED
    )

    merged.status = to_status
    merged.duplicate_of_ticket_id = None
    merged.routed_to_org_type = addressee.org_type
    merged.routed_to_org_id = addressee.org_id
    merged.due_at = (
        now + timedelta(hours=pt.resolution_hours)
        if pt is not None and pt.resolution_hours is not None
        else None
    )
    merged.updated_at = now

    session.add(
        TicketEvent(
            id=uuid.uuid4(),
            ticket_id=merged.id,
            event_type=TicketEventType.STATUS_CHANGED.value,
            from_status=TicketStatus.MERGED,
            to_status=to_status,
            actor=actor,
            payload=None,
            created_at=now,
        )
    )

    head = await session.get(Ticket, head_id) if head_id is not None else None
    if head is not None:
        # Автора головной заявки не отписываем: он подписан как автор, а не
        # склейкой, и остался бы без статуса собственного обращения.
        subscription = await session.get(TicketSubscriber, (head.id, merged.user_id))
        if subscription is not None and not subscription.is_author:
            await session.delete(subscription)
        session.add(
            TicketEvent(
                id=uuid.uuid4(),
                ticket_id=head.id,
                event_type=TicketEventType.SUBSCRIBER_LEFT.value,
                from_status=None,
                to_status=head.status,
                actor=actor,
                payload={"split_ticket_number": merged.number},
                created_at=now,
            )
        )

    await session.flush()
    return merged


async def get_by_source_event(session: AsyncSession, source_event_id: uuid.UUID) -> Ticket | None:
    """Заявка, уже созданная этим входящим событием (идемпотентность TICKET-001)."""

    return (
        await session.execute(select(Ticket).where(Ticket.source_event_id == source_event_id))
    ).scalar_one_or_none()


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
    """Открытые первыми, не больше limit.

    Склеенные обращения жителю не показываются: он подписан на головную
    заявку и видит её, а собственный дубль в списке выглядел бы второй
    заявкой о той же аварии (DEDUP-001).
    """

    rows = (
        (
            await session.execute(
                select(Ticket)
                .join(
                    TicketSubscriber,
                    TicketSubscriber.ticket_id == Ticket.id,
                )
                .where(
                    TicketSubscriber.user_id == user_id,
                    Ticket.status != TicketStatus.MERGED,
                )
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


def is_history_event(event: TicketEvent) -> bool:
    """Что из ленты показывается жителю.

    Служебное `routed` из `create_ticket` (ушли на УК, потому что РСО по
    ресурсу не нашлась) в ленту не попадает: для жителя ничего никуда не
    передавали. Отличается по ключу `to` — его пишет только `reroute`.
    События о подписчиках показываются отдельной агрегированной строкой, без
    имён и идентификаторов других жителей (architecture.md §11).
    """

    if event.event_type == TicketEventType.ROUTED.value:
        return isinstance(event.payload, dict) and "to" in event.payload
    return event.event_type in (
        TicketEventType.CREATED.value,
        TicketEventType.STATUS_CHANGED.value,
    )


async def list_history_events(
    session: AsyncSession, ticket_id: uuid.UUID, *, limit: int = 5
) -> list[TicketEvent]:
    """Последние события заявки в хронологическом порядке (STATUS-001).

    Отбор служебных событий делается в Python, а не в SQL: условие смотрит
    внутрь JSON `payload`, а событий на заявку единицы — выборка дешевле
    JSON-предиката в запросе.
    """

    rows = (
        (
            await session.execute(
                select(TicketEvent)
                .where(TicketEvent.ticket_id == ticket_id)
                .order_by(TicketEvent.created_at, TicketEvent.id)
            )
        )
        .scalars()
        .all()
    )
    return [event for event in rows if is_history_event(event)][-limit:]


async def count_joined_subscribers(session: AsyncSession, ticket_id: uuid.UUID) -> int:
    """Сколько жителей присоединилось к заявке помимо автора.

    Считается по текущим подписчикам, а не по событиям `subscriber_joined`:
    после выхода из склейки (DEDUP-001) число в ленте должно уменьшаться, а
    события append-only.
    """

    found = await session.scalar(
        select(func.count())
        .select_from(TicketSubscriber)
        .where(
            TicketSubscriber.ticket_id == ticket_id,
            TicketSubscriber.is_author.is_(False),
        )
    )
    return int(found or 0)


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
