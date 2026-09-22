"""Рассылка уведомлений об отключениях (NOTIFY-001, architecture.md §5.3).

Житель узнаёт о плановом отключении заранее, а об аварийном — сразу, не
заходя в ГИС ЖКХ или на Госуслуги. Источник данных в MVP — только
смоделированные записи и служебный CLI, реальной интеграции с РСО нет
(ограничение №10 кейса).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import outbox
from upravdom.config import Settings, get_settings
from upravdom.models import House, Notification
from upravdom.models.enums import NotificationType
from upravdom.notifications.texts import notification_body

logger = logging.getLogger(__name__)

# Одним запросом: кандидаты (дом + действующее согласие) сразу превращаются в
# доставки. `ON CONFLICT DO NOTHING` — гарантия «одному жителю одно
# уведомление»; `NOT EXISTS` — только оптимизация, чтобы batch не выбирался
# уже разосланными. Полагаться на проверку перед вставкой нельзя: между ней и
# вставкой помещается второй проход задачи (приём inbox из BOT-001).
_CLAIM_DELIVERIES = text(
    """
    INSERT INTO notification_deliveries (id, notification_id, user_id, status, created_at)
    SELECT gen_random_uuid(), :notification_id, candidate.user_id, 'pending', now()
    FROM (
        SELECT DISTINCT u.id AS user_id, u.max_user_id
        FROM users u
        JOIN user_houses uh ON uh.user_id = u.id
        JOIN consents c ON c.user_id = u.id
        WHERE uh.house_id = :house_id
          AND c.consent_version = :consent_version
          AND c.revoked_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM notification_deliveries d
              WHERE d.notification_id = :notification_id AND d.user_id = u.id
          )
        ORDER BY u.max_user_id
        LIMIT :batch_size
    ) AS candidate
    ON CONFLICT (notification_id, user_id) DO NOTHING
    RETURNING id, user_id
    """
)

_RECIPIENT_ADDRESSES = text("SELECT id, max_user_id FROM users WHERE id = ANY(:user_ids)")


@dataclass(slots=True, frozen=True)
class Delivery:
    id: uuid.UUID
    user_id: uuid.UUID
    max_user_id: str


async def list_active(session: AsyncSession, *, now: datetime | None = None) -> list[Notification]:
    """Уведомления, которые ещё имеет смысл рассылать.

    - завершённое (`ends_at` в прошлом) не рассылается никогда;
    - аварийное — пока действует;
    - плановое — только **до** начала: уведомление «заранее» после начала
      отключения новым получателям бессмысленно, они уже всё увидели сами.
    """

    now = now or datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(Notification)
                .where(
                    or_(Notification.ends_at.is_(None), Notification.ends_at > now),
                    or_(
                        Notification.type == NotificationType.EMERGENCY_OUTAGE,
                        and_(
                            Notification.type == NotificationType.PLANNED_OUTAGE,
                            Notification.starts_at > now,
                        ),
                    ),
                )
                .order_by(Notification.created_at)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def claim_deliveries(
    session: AsyncSession,
    notification: Notification,
    *,
    consent_version: int,
    batch_size: int,
) -> list[Delivery]:
    """Создаёт доставки для получателей, которым ещё не отправляли."""

    rows = (
        await session.execute(
            _CLAIM_DELIVERIES,
            {
                "notification_id": notification.id,
                "house_id": notification.house_id,
                "consent_version": consent_version,
                "batch_size": batch_size,
            },
        )
    ).all()
    if not rows:
        return []

    user_ids = [row.user_id for row in rows]
    addresses = {
        row.id: row.max_user_id
        for row in (await session.execute(_RECIPIENT_ADDRESSES, {"user_ids": user_ids})).all()
    }
    return [
        Delivery(id=row.id, user_id=row.user_id, max_user_id=addresses[row.user_id])
        for row in rows
        if row.user_id in addresses
    ]


async def dispatch(
    session: AsyncSession,
    notification: Notification,
    *,
    settings: Settings | None = None,
) -> int:
    """Ставит уведомление в очередь всем новым получателям. Возвращает их число.

    Не коммитит: проход воркера фиксирует одно уведомление одной транзакцией
    (`scheduler.process_notifications`), чтобы сбой на одном доме не
    блокировал остальные.
    """

    settings = settings or get_settings()
    deliveries = await claim_deliveries(
        session,
        notification,
        consent_version=settings.consent_version,
        batch_size=settings.notify_batch_size,
    )
    if not deliveries:
        return 0

    house = await session.get(House, notification.house_id)
    body = notification_body(
        type_=notification.type,
        resource=notification.resource_type,
        address=house.address_raw if house else "ваш дом",
        starts_at=notification.starts_at,
        ends_at=notification.ends_at,
        message=notification.message,
        source=notification.source,
        tz_name=settings.display_timezone,
    )
    for delivery in deliveries:
        await outbox.enqueue_message(
            session,
            user_id=delivery.max_user_id,
            text_=body,
            notification_delivery_id=delivery.id,
        )
    return len(deliveries)
