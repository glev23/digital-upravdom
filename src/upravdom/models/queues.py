"""Durable inbox/outbox — без брокера (architecture.md §3, §5.4, §7)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from upravdom.models.base import Base, UUIDPKMixin
from upravdom.models.enums import InboundEventStatus, OutboundMessageStatus, pg_enum


class InboundEvent(UUIDPKMixin, Base):
    """Идемпотентность по `max_event_id` — уникальность гарантирует БД."""

    __tablename__ = "inbound_events"

    max_event_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[InboundEventStatus] = mapped_column(
        pg_enum(InboundEventStatus, name="ck_inbound_events_status"),
        nullable=False,
        default=InboundEventStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Добавлено в BOT-001 (миграция 0002): без метки начала обработки
    # невозможно отличить свежую `processing`-строку от зависшей после
    # аварийной остановки воркера — architecture.md §3 требует именно такое
    # восстановление, а в ревизии DB-001 колонки не было.
    processing_started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # ONBOARD-001 (миграция 0003): найти удержанные до согласия события
    # жителя без разбора JSON `payload` в каждом запросе.
    max_user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


class OutboundMessage(UUIDPKMixin, Base):
    """`created_at` добавлено сверх architecture.md §5.4 для сортировки очереди
    (реализационное дополнение, см. «История решений» architecture.md)."""

    __tablename__ = "outbound_messages"

    max_chat_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[OutboundMessageStatus] = mapped_column(
        pg_enum(OutboundMessageStatus, name="ck_outbound_messages_status"),
        nullable=False,
        default=OutboundMessageStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
