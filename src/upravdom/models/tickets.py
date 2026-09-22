"""Справочник типов проблем и заявки (architecture.md §4, §5.2, §6.5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from upravdom.models.base import Base, UUIDPKMixin
from upravdom.models.enums import JoinReason, ResponsibilityZone, TicketStatus, pg_enum


class ProblemType(Base):
    """Справочник типов проблем — данные, не код (architecture.md §4).

    Региональная адаптация нормативных сроков — новый seed, а не правка кода.
    """

    __tablename__ = "problem_types"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    default_responsibility_zone: Mapped[ResponsibilityZone] = mapped_column(
        pg_enum(ResponsibilityZone, name="ck_problem_types_default_responsibility_zone"),
        nullable=False,
    )
    # Нет фиксированного значения по умолчанию: где норматив не задаёт
    # однозначный срок, это NULL + объяснение в norm_reference, а не
    # приблизительная цифра (db-001.md, «Справочник problem_types»).
    resolution_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    norm_reference: Mapped[str] = mapped_column(String, nullable=False)


class Ticket(UUIDPKMixin, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_tickets_confidence_range"),
    )

    # Короткий номер для жителя («№ 1024») — TICKET-001, миграция 0005.
    number: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        unique=True,
        server_default=text("nextval('ticket_number_seq')"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    house_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("houses.id"), nullable=False)
    # Арендное (tenant) измерение — nullable: дом мог ещё не получить УК
    # (houses.management_company_id тоже nullable).
    management_company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("management_companies.id"), nullable=True
    )
    raw_text: Mapped[str] = mapped_column(String, nullable=False)
    problem_type: Mapped[str] = mapped_column(ForeignKey("problem_types.code"), nullable=False)
    responsibility_zone: Mapped[ResponsibilityZone] = mapped_column(
        pg_enum(ResponsibilityZone, name="ck_tickets_responsibility_zone"),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    # Полиморфный адресат (management_companies ИЛИ resource_organizations) —
    # без FK намеренно; тип различается через routed_to_org_type.
    routed_to_org_type: Mapped[ResponsibilityZone | None] = mapped_column(
        pg_enum(ResponsibilityZone, name="ck_tickets_routed_to_org_type"),
        nullable=True,
    )
    routed_to_org_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    status: Mapped[TicketStatus] = mapped_column(
        pg_enum(TicketStatus, name="ck_tickets_status"),
        nullable=False,
        default=TicketStatus.ACCEPTED,
    )
    due_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Задел DB-001 под вектор в Qdrant. Остаётся неиспользуемым: дедупликация
    # сравнивает векторы заявок одного дома напрямую в Postgres, чтобы
    # недоступность Qdrant её не ломала (architecture.md §6.5, §9).
    embedding_point_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # Эмбеддинг маскированного текста обращения, dim 768 (DEDUP-001, 0007).
    text_embedding: Mapped[list[float] | None] = mapped_column(ARRAY(REAL), nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inbound_events.id"), nullable=True
    )
    # Дедупликация (architecture.md §6.5): дубликат остаётся строкой со
    # статусом MERGED, а не исчезает.
    dedup_group_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    duplicate_of_ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tickets.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class TicketEvent(UUIDPKMixin, Base):
    """Append-only история статусов (architecture.md §5.2).

    `UPDATE`/`DELETE` запрещены триггером на уровне БД — см. миграцию
    `0001_initial_schema` — иначе гарантию app-уровня легко обойти.
    """

    __tablename__ = "ticket_events"

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    from_status: Mapped[TicketStatus | None] = mapped_column(
        pg_enum(TicketStatus, name="ck_ticket_events_from_status"),
        nullable=True,
    )
    to_status: Mapped[TicketStatus] = mapped_column(
        pg_enum(TicketStatus, name="ck_ticket_events_to_status"),
        nullable=False,
    )
    actor: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class TicketSubscriber(Base):
    """Подписчики заявки — следствие дедупликации (architecture.md §6.5)."""

    __tablename__ = "ticket_subscribers"

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    is_author: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    joined_at: Mapped[datetime] = mapped_column(nullable=False)
    join_reason: Mapped[JoinReason] = mapped_column(
        pg_enum(JoinReason, name="ck_ticket_subscribers_join_reason"),
        nullable=False,
    )
