"""Уведомления об отключениях (architecture.md §5.3)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from upravdom.models.base import Base, CreatedAtMixin, UUIDPKMixin
from upravdom.models.enums import (
    DeliveryStatus,
    NotificationSource,
    NotificationType,
    ResourceType,
    pg_enum,
)


class Notification(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "notifications"

    house_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("houses.id"), nullable=False)
    type: Mapped[NotificationType] = mapped_column(
        pg_enum(NotificationType, name="ck_notifications_type"),
        nullable=False,
    )
    resource_type: Mapped[ResourceType] = mapped_column(
        pg_enum(ResourceType, name="ck_notifications_resource_type"),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(nullable=True)
    message: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[NotificationSource] = mapped_column(
        pg_enum(NotificationSource, name="ck_notifications_source"),
        nullable=False,
    )


class NotificationDelivery(UUIDPKMixin, Base):
    """Кому фактически доставлено — задел под повтор неудачных отправок.

    `created_at` добавлено сверх architecture.md §5.3 для сортировки повторов
    (учтено в «Истории решений» architecture.md как реализационное дополнение).
    """

    __tablename__ = "notification_deliveries"

    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        pg_enum(DeliveryStatus, name="ck_notification_deliveries_status"),
        nullable=False,
        default=DeliveryStatus.PENDING,
    )
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
