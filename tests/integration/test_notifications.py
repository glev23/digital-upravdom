"""Сценарии 1–10 NOTIFY-001 на реальном Postgres.

Отправка проверяется через очередь `outbound_messages` и `FakeMaxClient` —
в сеть тесты не ходят.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from scripts.seed_demo import seed, stable_id
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import outbox
from upravdom.bot_gateway.inbox import get_or_create_user
from upravdom.bot_gateway.max_client import FakeMaxClient
from upravdom.config import get_settings
from upravdom.models import Notification, NotificationDelivery
from upravdom.models.enums import (
    DeliveryStatus,
    NotificationSource,
    NotificationType,
    ResourceType,
)
from upravdom.notifications import texts as notify_texts
from upravdom.notifications.service import dispatch, list_active
from upravdom.onboarding import service as onboarding_service

pytestmark = pytest.mark.asyncio

HOUSE_A = "house:house-dekabristov-10"
HOUSE_B = "house:house-lumumby-5"


@pytest.fixture
async def houses(session: AsyncSession) -> None:
    await seed(session)
    await session.commit()


async def _resident(session: AsyncSession, *house_keys: str, consent: bool = True) -> Any:
    user = await get_or_create_user(session, f"ntf-{uuid.uuid4().hex[:10]}")
    for key in house_keys:
        await onboarding_service.bind_house(session, user.id, stable_id(key))
    if consent:
        await onboarding_service.grant_consent(
            session, user.id, version=get_settings().consent_version, source="test"
        )
    await session.flush()
    return user


async def _notification(
    session: AsyncSession,
    *,
    house_key: str = HOUSE_A,
    type_: NotificationType = NotificationType.EMERGENCY_OUTAGE,
    resource: ResourceType = ResourceType.ELECTRICITY,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    message: str = "Ведутся работы.",
    source: NotificationSource = NotificationSource.MANUAL,
) -> Notification:
    notification = Notification(
        id=uuid.uuid4(),
        house_id=stable_id(house_key),
        type=type_,
        resource_type=resource,
        starts_at=starts_at or (datetime.now(UTC) - timedelta(minutes=30)),
        ends_at=ends_at,
        message=message,
        source=source,
        created_at=datetime.now(UTC),
    )
    session.add(notification)
    await session.flush()
    return notification


async def _messages_for(session: AsyncSession, max_user_id: str) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT payload FROM outbound_messages "
            "WHERE payload::jsonb ->> 'user_id' = :u ORDER BY created_at, id"
        ),
        {"u": max_user_id},
    )
    return [row[0]["text"] for row in rows]


async def _deliveries(session: AsyncSession, notification: Notification) -> list[Any]:
    # `populate_existing`: статус доставки обновляет `outbox.send_claimed`
    # сырым SQL, и объекты в identity map остались бы со старым значением.
    return list(
        (
            await session.execute(
                select(NotificationDelivery)
                .where(NotificationDelivery.notification_id == notification.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _drain_outbox(session: AsyncSession, client: FakeMaxClient) -> None:
    """Как воркер: захват пачки и отправка через клиента."""

    settings = get_settings()
    claimed = await outbox.claim_batch(session, batch_size=settings.worker_batch_size)
    for message in claimed:
        await outbox.send_claimed(
            session,
            message,
            client=client,
            max_attempts=settings.outbound_max_attempts,
            backoff_base_seconds=settings.outbound_backoff_base_seconds,
        )


# --- 1 -----------------------------------------------------------------------


async def test_emergency_reaches_both_residents(session: AsyncSession, houses: None) -> None:
    first = await _resident(session, HOUSE_A)
    second = await _resident(session, HOUSE_A)
    notification = await _notification(session)

    assert await dispatch(session, notification) == 2

    assert len(await _messages_for(session, first.max_user_id)) == 1
    assert len(await _messages_for(session, second.max_user_id)) == 1
    deliveries = await _deliveries(session, notification)
    assert len(deliveries) == 2
    assert all(d.status is DeliveryStatus.PENDING for d in deliveries)

    await session.commit()
    client = FakeMaxClient()
    await _drain_outbox(session, client)

    deliveries = await _deliveries(session, notification)
    assert len(deliveries) == 2
    assert all(d.status is DeliveryStatus.SENT and d.sent_at is not None for d in deliveries)
    assert all(user_id is not None for user_id in client.sent_user_ids)


# --- 2 -----------------------------------------------------------------------


async def test_second_pass_sends_nothing_new(session: AsyncSession, houses: None) -> None:
    resident = await _resident(session, HOUSE_A)
    notification = await _notification(session)

    assert await dispatch(session, notification) == 1
    assert await dispatch(session, notification) == 0
    assert await dispatch(session, notification) == 0

    assert len(await _messages_for(session, resident.max_user_id)) == 1
    assert len(await _deliveries(session, notification)) == 1


async def test_unique_constraint_guards_against_race(session: AsyncSession, houses: None) -> None:
    """Гарантия — ограничение БД, а не проверка перед вставкой."""

    resident = await _resident(session, HOUSE_A)
    notification = await _notification(session)
    await dispatch(session, notification)
    await session.flush()

    with pytest.raises(Exception, match="uq_notification_deliveries_notification_user"):
        session.add(
            NotificationDelivery(
                id=uuid.uuid4(),
                notification_id=notification.id,
                user_id=resident.id,
                status=DeliveryStatus.PENDING,
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()


# --- 3 -----------------------------------------------------------------------


async def test_no_consent_no_message(session: AsyncSession, houses: None) -> None:
    with_consent = await _resident(session, HOUSE_A)
    without = await _resident(session, HOUSE_A, consent=False)
    revoked = await _resident(session, HOUSE_A)
    await onboarding_service.revoke_consent(session, revoked.id)
    notification = await _notification(session)

    assert await dispatch(session, notification) == 1

    assert len(await _messages_for(session, with_consent.max_user_id)) == 1
    assert await _messages_for(session, without.max_user_id) == []
    assert await _messages_for(session, revoked.max_user_id) == []
    assert len(await _deliveries(session, notification)) == 1


# --- 4 -----------------------------------------------------------------------


async def test_resident_bound_during_outage_gets_it_on_next_pass(
    session: AsyncSession, houses: None
) -> None:
    early = await _resident(session, HOUSE_A)
    notification = await _notification(session)
    assert await dispatch(session, notification) == 1

    late = await _resident(session, HOUSE_A)
    assert await dispatch(session, notification) == 1

    assert len(await _messages_for(session, early.max_user_id)) == 1
    assert len(await _messages_for(session, late.max_user_id)) == 1


# --- 5, 6, 7 ------------------------------------------------------------------


async def test_planned_outage_is_sent_in_advance(session: AsyncSession, houses: None) -> None:
    resident = await _resident(session, HOUSE_A)
    now = datetime.now(UTC)
    notification = await _notification(
        session,
        type_=NotificationType.PLANNED_OUTAGE,
        resource=ResourceType.COLD_WATER,
        starts_at=now + timedelta(days=2),
        ends_at=now + timedelta(days=2, hours=8),
    )

    assert notification in await list_active(session)
    assert await dispatch(session, notification) == 1
    body = (await _messages_for(session, resident.max_user_id))[0]
    assert body.startswith("Плановое отключение: холодное водоснабжение")


async def test_started_planned_outage_is_not_sent_to_new_recipients(
    session: AsyncSession, houses: None
) -> None:
    await _resident(session, HOUSE_A)
    now = datetime.now(UTC)
    notification = await _notification(
        session,
        type_=NotificationType.PLANNED_OUTAGE,
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=5),
    )

    assert notification not in await list_active(session)


async def test_finished_outage_is_never_sent(session: AsyncSession, houses: None) -> None:
    await _resident(session, HOUSE_A)
    now = datetime.now(UTC)
    finished = await _notification(session, ends_at=now - timedelta(minutes=1))

    assert finished not in await list_active(session)


# --- 8 -----------------------------------------------------------------------


async def test_delivery_status_follows_real_send(session: AsyncSession, houses: None) -> None:
    resident = await _resident(session, HOUSE_A)
    notification = await _notification(session)
    await dispatch(session, notification)
    await session.commit()

    # MAX недоступен: доставка остаётся pending, повтор делает сама очередь.
    flaky = FakeMaxClient(fail_times=1)
    await _drain_outbox(session, flaky)
    assert (await _deliveries(session, notification))[0].status is DeliveryStatus.PENDING

    await session.execute(text("UPDATE outbound_messages SET next_attempt_at = now()"))
    await session.commit()
    await _drain_outbox(session, flaky)
    delivery = (await _deliveries(session, notification))[0]
    assert delivery.status is DeliveryStatus.SENT
    assert len(await _messages_for(session, resident.max_user_id)) == 1


async def test_delivery_fails_when_attempts_exhausted(session: AsyncSession, houses: None) -> None:
    await _resident(session, HOUSE_A)
    notification = await _notification(session)
    await dispatch(session, notification)
    await session.commit()

    await _drain_outbox(session, FakeMaxClient(permanent_failure=True))

    delivery = (await _deliveries(session, notification))[0]
    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.sent_at is None
    # В `error` — тип исключения, не текст внешнего сервиса (architecture.md §11).
    assert delivery.error == "MaxPermanentError"


# --- 9 -----------------------------------------------------------------------


async def test_notification_for_secondary_house_is_delivered(
    session: AsyncSession, houses: None
) -> None:
    resident = await _resident(session, HOUSE_A, HOUSE_B)
    notification = await _notification(session, house_key=HOUSE_B)

    assert await dispatch(session, notification) == 1
    assert len(await _messages_for(session, resident.max_user_id)) == 1


# --- 10 ----------------------------------------------------------------------


async def test_seed_does_not_resurrect_closed_outage(session: AsyncSession) -> None:
    """`notify.py close` + рестарт контейнера: авария остаётся завершённой."""

    await seed(session)
    await session.commit()
    emergency = (
        await session.execute(
            select(Notification).where(
                Notification.type == NotificationType.EMERGENCY_OUTAGE,
                Notification.source == NotificationSource.TEST_DATA,
            )
        )
    ).scalar_one()
    closed_at = datetime.now(UTC)
    emergency.ends_at = closed_at
    await session.commit()

    await seed(session)  # как старт контейнера
    await session.commit()

    await session.refresh(emergency)
    assert emergency.ends_at is not None
    assert emergency not in await list_active(session)
    total = await session.scalar(select(func.count()).select_from(Notification))
    assert total == 2  # идемпотентность DATA-001 сохранилась


# --- текст -------------------------------------------------------------------


async def test_test_data_is_marked_for_the_resident(session: AsyncSession, houses: None) -> None:
    """Ограничение №10 кейса: смоделированное обозначается явно."""

    resident = await _resident(session, HOUSE_A)
    notification = await _notification(session, source=NotificationSource.TEST_DATA)

    await dispatch(session, notification)

    body = (await _messages_for(session, resident.max_user_id))[0]
    assert body.endswith(notify_texts.TEST_DATA_MARK)
    assert "Аварийное отключение: электроснабжение" in body
    assert "г. Казань, ул. Декабристов, д. 10" in body


async def test_manual_notification_has_no_test_mark(session: AsyncSession, houses: None) -> None:
    resident = await _resident(session, HOUSE_A)
    notification = await _notification(session, source=NotificationSource.MANUAL)

    await dispatch(session, notification)

    assert (
        notify_texts.TEST_DATA_MARK not in (await _messages_for(session, resident.max_user_id))[0]
    )
