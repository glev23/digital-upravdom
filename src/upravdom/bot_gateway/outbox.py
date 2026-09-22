"""Исходящая очередь с повторами (architecture.md §7).

`outbound_messages` не имеет отдельного статуса "в отправке" (только
`pending`/`sent`/`failed` — architecture.md §5.4). Захват реализован без
добавления колонки: `next_attempt_at` сдвигается в будущее атомарно вместе
с выборкой (`FOR UPDATE SKIP LOCKED` + `UPDATE ... RETURNING`), что разом
даёт короткий "лизинг" от параллельных воркеров и естественную точку
повтора, если процесс упадёт между захватом и отправкой — строка сама
станет доступна для захвата, как только `next_attempt_at` истечёт.

Обработчики никогда не отправляют в MAX напрямую — только вызывают
`enqueue_message` (architecture.md §7).
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway.max_client import MaxClient, MaxPermanentError, MaxTransientError
from upravdom.models import OutboundMessage
from upravdom.models.enums import DeliveryStatus, OutboundMessageStatus

# Лизинг на время захвата — с запасом над таймаутом HTTP-клиента (10 с),
# чтобы воркер не "отобрал у себя" ещё не отправленное сообщение.
_LEASE_SECONDS = 30


@dataclasses.dataclass(slots=True, frozen=True)
class ClaimedMessage:
    id: uuid.UUID
    max_chat_id: str
    payload: dict[str, Any]
    attempts: int

    @property
    def text(self) -> str:
        return str(self.payload.get("text", ""))

    @property
    def user_id(self) -> str | None:
        """Адресация по `user_id` вместо `chat_id` (STATUS-001) — см. `enqueue_message`."""

        value = self.payload.get("user_id")
        return str(value) if value else None

    @property
    def notification_delivery_id(self) -> uuid.UUID | None:
        """Доставка уведомления, статус которой зависит от результата отправки
        (NOTIFY-001) — см. `enqueue_message`."""

        value = self.payload.get("notification_delivery_id")
        if not value:
            return None
        try:
            return uuid.UUID(str(value))
        except ValueError:
            return None


async def _enqueue(session: AsyncSession, *, address: str, payload: dict[str, object]) -> None:
    # Без commit: запись фиксируется вместе с остальными эффектами
    # обработчика и отметкой события `done` (scheduler.process_inbound_batch)
    # — при сбое обработчика откатывается и она, ответ-полуфабрикат не уходит.
    # Core `insert()`, не `text()`: asyncpg не сериализует dict в JSON-параметр
    # голого SQL (наступили на это в BOT-001).
    stmt = pg_insert(OutboundMessage).values(
        id=uuid.uuid4(),
        max_chat_id=address,
        payload=payload,
        status=OutboundMessageStatus.PENDING,
        attempts=0,
        created_at=datetime.now(UTC),
    )
    await session.execute(stmt)


async def enqueue_message(
    session: AsyncSession,
    *,
    chat_id: str | None = None,
    user_id: str | None = None,
    text_: str,
    attachments: list[dict[str, object]] | None = None,
    notification_delivery_id: uuid.UUID | None = None,
) -> None:
    """Единственный способ отправить сообщение в MAX. Фиксирует вызывающий код.

    Адресат — ровно один из двух (`POST /messages`, max_api.md §7). Ответ в
    текущем диалоге идёт по `chat_id` из события; уведомление подписчику
    заявки — по `user_id` (STATUS-001): в `users` хранится только
    `max_user_id`, и `chat_id` диалога с ботом ему не равен.

    `outbound_messages.max_chat_id` — ключ доставки и порядка в очереди, он
    NOT NULL, поэтому при адресации по жителю туда кладётся тот же
    `max_user_id`; способ адресации различается по ключу `user_id` в payload
    (тот же приём, что у `callback_id` ниже) — новой колонки и миграции это
    не требует.

    `notification_delivery_id` (NOTIFY-001) связывает сообщение с записью
    `notification_deliveries`: её статус должен отражать **реальную** отправку
    в MAX, а не факт постановки в очередь, поэтому переводит доставку в
    `sent`/`failed` именно `send_claimed`.
    """

    payload: dict[str, object] = {"text": text_}
    if attachments:
        payload["attachments"] = attachments
    if user_id is not None:
        payload["user_id"] = user_id
    if notification_delivery_id is not None:
        payload["notification_delivery_id"] = str(notification_delivery_id)
    await _enqueue(session, address=_address(chat_id, user_id), payload=payload)


async def enqueue_callback_answer(
    session: AsyncSession, *, chat_id: str, callback_id: str, notification: str
) -> None:
    """Ответ на нажатие кнопки (`POST /answers`) — через ту же очередь с повторами."""

    await _enqueue(
        session,
        address=chat_id,
        payload={"callback_id": callback_id, "notification": notification},
    )


def _address(chat_id: str | None, user_id: str | None) -> str:
    if (chat_id is None) == (user_id is None):
        msg = "нужен ровно один адресат: chat_id или user_id"
        raise ValueError(msg)
    return chat_id if chat_id is not None else str(user_id)


async def claim_batch(session: AsyncSession, *, batch_size: int) -> list[ClaimedMessage]:
    result = await session.execute(
        text(
            """
            WITH claimed AS (
                SELECT id FROM outbound_messages
                WHERE status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                ORDER BY created_at
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            UPDATE outbound_messages
            SET attempts = attempts + 1,
                next_attempt_at = now() + make_interval(secs => :lease_seconds)
            FROM claimed
            WHERE outbound_messages.id = claimed.id
            RETURNING outbound_messages.id, outbound_messages.max_chat_id,
                      outbound_messages.payload, outbound_messages.attempts
            """
        ),
        {"batch_size": batch_size, "lease_seconds": _LEASE_SECONDS},
    )
    await session.commit()
    return [
        ClaimedMessage(
            id=row.id,
            max_chat_id=row.max_chat_id,
            payload=row.payload,
            attempts=row.attempts,
        )
        for row in result
    ]


def _backoff_seconds(attempts: int, *, base_seconds: float) -> float:
    """Экспоненциальный backoff: base * 2^(attempts-1), без верхней границы
    выше здравого смысла — воркер всё равно опрашивает раз в poll-interval."""

    return float(base_seconds * (2 ** max(attempts - 1, 0)))


async def _mark_delivery(
    session: AsyncSession,
    delivery_id: uuid.UUID | None,
    *,
    status: DeliveryStatus,
    error: str | None = None,
) -> None:
    """Статус доставки уведомления по результату реальной отправки (NOTIFY-001).

    В `error` — тип исключения, не его текст: в сообщении внешнего сервиса
    может оказаться что угодно, включая payload (architecture.md §11).
    """

    if delivery_id is None:
        return
    values: dict[str, object] = {"id": delivery_id, "status": status.value, "error": error}
    sent_at = "now()" if status is DeliveryStatus.SENT else "NULL"
    await session.execute(
        text(
            "UPDATE notification_deliveries "
            f"SET status = :status, error = :error, sent_at = {sent_at} WHERE id = :id"
        ),
        values,
    )


async def _mark_sent(session: AsyncSession, message: ClaimedMessage) -> None:
    await session.execute(
        text("UPDATE outbound_messages SET status = 'sent', sent_at = now() WHERE id = :id"),
        {"id": message.id},
    )
    await _mark_delivery(session, message.notification_delivery_id, status=DeliveryStatus.SENT)
    await session.commit()


async def _mark_retry(
    session: AsyncSession, message_id: uuid.UUID, *, delay_seconds: float, error: str
) -> None:
    next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    await session.execute(
        text(
            "UPDATE outbound_messages "
            "SET next_attempt_at = :next_attempt_at, last_error = :error WHERE id = :id"
        ),
        {"id": message_id, "next_attempt_at": next_attempt_at, "error": error},
    )
    await session.commit()


async def _mark_failed(
    session: AsyncSession, message: ClaimedMessage, *, error: str, error_type: str
) -> None:
    await session.execute(
        text("UPDATE outbound_messages SET status = 'failed', last_error = :error WHERE id = :id"),
        {"id": message.id, "error": error},
    )
    await _mark_delivery(
        session,
        message.notification_delivery_id,
        status=DeliveryStatus.FAILED,
        error=error_type,
    )
    await session.commit()


async def send_claimed(
    session: AsyncSession,
    message: ClaimedMessage,
    *,
    client: MaxClient,
    max_attempts: int,
    backoff_base_seconds: float,
) -> None:
    """Отправляет одно захваченное сообщение и фиксирует результат.

    Ошибка отправки не поднимается наружу — воркер не должен падать
    из-за одного плохого сообщения (architecture.md §7, критерий приёмки).
    """

    try:
        callback_id = message.payload.get("callback_id")
        if callback_id:
            await client.answer_callback(
                callback_id=str(callback_id),
                notification=str(message.payload.get("notification", "")),
            )
        elif message.user_id is not None:
            await client.send_message(
                user_id=message.user_id,
                text=message.text,
                attachments=message.payload.get("attachments"),
            )
        else:
            await client.send_message(
                chat_id=message.max_chat_id,
                text=message.text,
                attachments=message.payload.get("attachments"),
            )
    except MaxPermanentError as exc:
        await _mark_failed(session, message, error=str(exc)[:500], error_type=type(exc).__name__)
        return
    except MaxTransientError as exc:
        if message.attempts >= max_attempts:
            await _mark_failed(
                session, message, error=str(exc)[:500], error_type=type(exc).__name__
            )
        else:
            # Доставка остаётся `pending`: повторы делает сама очередь, свой
            # второй механизм повторов уведомлениям не нужен (NOTIFY-001).
            delay = _backoff_seconds(message.attempts, base_seconds=backoff_base_seconds)
            await _mark_retry(session, message.id, delay_seconds=delay, error=str(exc)[:500])
        return
    await _mark_sent(session, message)
