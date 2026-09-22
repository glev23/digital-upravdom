"""Служебное управление уведомлениями об отключениях (NOTIFY-001).

Не HTTP — по architecture.md §8 объявленный API порождает обязательства
сдачи; для демонстрации достаточно команды в контейнере:

  docker compose exec app python scripts/notify.py list
  docker compose exec app python scripts/notify.py create --house <id|токен> \
      --type planned --resource hot_water --starts "2026-09-24 09:00" \
      --ends "2026-09-24 17:00" --message "Профилактика на ЦТП"
  docker compose exec app python scripts/notify.py close <notification_id>

Время в `--starts`/`--ends` — в `DISPLAY_TIMEZONE` (по умолчанию
Europe/Moscow): диспетчер вводит местное время, а не UTC.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.config import get_settings
from upravdom.db import get_engine, session_scope
from upravdom.models import House, HouseLink, Notification, NotificationDelivery
from upravdom.models.enums import (
    DeliveryStatus,
    NotificationSource,
    NotificationType,
    ResourceType,
)
from upravdom.notifications.texts import resource_label
from upravdom.tickets.due import display_zone, local_short

_TYPES = {
    "planned": NotificationType.PLANNED_OUTAGE,
    "emergency": NotificationType.EMERGENCY_OUTAGE,
}
_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M")


def _parse_moment(value: str, tz_name: str) -> datetime:
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=display_zone(tz_name))
        except ValueError:
            continue
    msg = f"не разобрал время {value!r}; ожидаю, например, «2026-09-24 09:00»"
    raise argparse.ArgumentTypeError(msg)


async def _resolve_house(session: AsyncSession, raw: str) -> uuid.UUID | None:
    """Принимает и UUID дома, и токен deep-link — по токену проще с демо-стенда."""

    try:
        house_id = uuid.UUID(raw)
    except ValueError:
        return await session.scalar(select(HouseLink.house_id).where(HouseLink.token == raw))
    found = await session.scalar(select(House.id).where(House.id == house_id))
    return found


async def _create(args: argparse.Namespace) -> int:
    tz_name = get_settings().display_timezone
    async with session_scope() as session:
        house_id = await _resolve_house(session, args.house)
        if house_id is None:
            print(f"дом не найден: {args.house}", file=sys.stderr)
            return 1
        notification = Notification(
            id=uuid.uuid4(),
            house_id=house_id,
            type=_TYPES[args.type],
            resource_type=ResourceType(args.resource),
            starts_at=_parse_moment(args.starts, tz_name),
            ends_at=_parse_moment(args.ends, tz_name) if args.ends else None,
            message=args.message,
            source=NotificationSource.MANUAL,
            created_at=datetime.now(UTC),
        )
        session.add(notification)
        await session.commit()
        print(f"{notification.id}: {args.type} {args.resource}, дом {house_id}")
    return 0


async def _close(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        notification = await session.get(Notification, uuid.UUID(args.notification_id))
        if notification is None:
            print(f"уведомление не найдено: {args.notification_id}", file=sys.stderr)
            return 1
        notification.ends_at = datetime.now(UTC)
        await session.commit()
        print(f"{notification.id}: завершено")
    return 0


async def _list(_args: argparse.Namespace) -> int:
    tz_name = get_settings().display_timezone
    now = datetime.now(UTC)
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(Notification).order_by(Notification.created_at.desc()).limit(50)
                )
            )
            .scalars()
            .all()
        )
        for notification in rows:
            counts = {
                status.value: 0
                for status in (DeliveryStatus.SENT, DeliveryStatus.PENDING, DeliveryStatus.FAILED)
            }
            for status, count in (
                await session.execute(
                    select(NotificationDelivery.status, func.count())
                    .where(NotificationDelivery.notification_id == notification.id)
                    .group_by(NotificationDelivery.status)
                )
            ).all():
                counts[status.value] = count
            ends = local_short(notification.ends_at, tz_name) if notification.ends_at else "—"
            active = notification.ends_at is None or notification.ends_at > now
            print(
                f"{notification.id}  {'действует' if active else 'завершено':9}  "
                f"{notification.type.value:17} {resource_label(notification.resource_type):22} "
                f"{local_short(notification.starts_at, tz_name)} — {ends}  "
                f"sent={counts['sent']} pending={counts['pending']} failed={counts['failed']}"
            )
        if not rows:
            print("уведомлений нет")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Служебное управление уведомлениями")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="создать уведомление (source = manual)")
    create.add_argument("--house", required=True, help="UUID дома или токен deep-link")
    create.add_argument("--type", required=True, choices=sorted(_TYPES))
    create.add_argument("--resource", required=True, choices=[r.value for r in ResourceType])
    create.add_argument("--starts", required=True, help="например «2026-09-24 09:00»")
    create.add_argument("--ends", default=None, help="окончание; для аварии можно не указывать")
    create.add_argument("--message", default="", help="пояснение от УК/РСО")
    create.set_defaults(handler=_create)

    close = sub.add_parser("close", help="завершить аварийное отключение")
    close.add_argument("notification_id")
    close.set_defaults(handler=_close)

    listing = sub.add_parser("list", help="уведомления и счётчики доставок")
    listing.set_defaults(handler=_list)

    args = parser.parse_args()

    async def _run() -> int:
        try:
            return int(await args.handler(args))
        finally:
            await get_engine().dispose()

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
