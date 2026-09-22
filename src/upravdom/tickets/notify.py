"""Рассылка подписчикам заявки (STATUS-001, architecture.md §5.2, §7, §11.1).

Сообщение ставится в `outbox` в транзакции вызывающего: статус без
уведомления или уведомление без статуса — рассинхрон, который житель увидит
(«сказали, что выполнено, а в статусе — в работе»). Прямая отправка в MAX
из обработчика запрещена (§7).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import outbox
from upravdom.config import Settings, get_settings
from upravdom.models import Consent, Ticket, TicketSubscriber, User

# Импорт подмодуля, а не `from upravdom.tickets import texts`: `tickets/__init__`
# в этот момент ещё инициализируется (service → notify), и атрибута пакета пока нет.
from upravdom.tickets.texts import BTN_STATUS


async def subscriber_max_user_ids(
    session: AsyncSession, ticket: Ticket, *, consent_version: int
) -> list[str]:
    """Подписчики с действующим согласием актуальной версии — и только они.

    После отзыва согласия бот жителю больше не пишет (architecture.md §11.1),
    поэтому фильтр стоит в самом запросе получателей, а не в вызывающем коде.
    """

    rows = await session.execute(
        select(User.max_user_id)
        .join(TicketSubscriber, TicketSubscriber.user_id == User.id)
        .join(Consent, Consent.user_id == User.id)
        .where(
            TicketSubscriber.ticket_id == ticket.id,
            Consent.consent_version == consent_version,
            Consent.revoked_at.is_(None),
        )
        .distinct()
        .order_by(User.max_user_id)
    )
    return [row.max_user_id for row in rows]


def status_keyboard(ticket_number: int) -> list[dict[str, object]]:
    """Кнопка «Статус заявки» под уведомлением.

    Импорт локальный: `flow` зависит от `tickets` (flow/handlers), поэтому
    импорт `upravdom.flow.*` на уровне модуля замкнул бы цикл через
    `upravdom.flow.__init__`. Формат `clf:st:<номер>` принадлежит FLOW-001 —
    дублировать строку кнопки здесь нельзя, разъедется при первой правке.
    """

    from upravdom.flow.callbacks import encode_status
    from upravdom.onboarding.callbacks import inline_keyboard

    return inline_keyboard([[(BTN_STATUS, encode_status(ticket_number))]])


async def notify_subscribers(
    session: AsyncSession,
    ticket: Ticket,
    text_: str,
    *,
    with_status_button: bool = True,
    settings: Settings | None = None,
) -> int:
    """Ставит уведомление в очередь каждому подписчику. Возвращает число адресатов."""

    settings = settings or get_settings()
    recipients = await subscriber_max_user_ids(
        session, ticket, consent_version=settings.consent_version
    )
    attachments = status_keyboard(ticket.number) if with_status_button else None
    for max_user_id in recipients:
        await outbox.enqueue_message(
            session, user_id=max_user_id, text_=text_, attachments=attachments
        )
    return len(recipients)
