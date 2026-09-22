"""Кнопка «Это другая проблема» (DEDUP-001, architecture.md §6.5).

Выход из склейки — Must Have внутри Should Have: ложная склейка дороже
пропущенного дубля. Пропущенный дубль стоит диспетчеру лишней строки, а
ложная склейка означает, что житель считает проблему принятой, тогда как её
потеряли.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import outbox
from upravdom.bot_gateway.inbox import ClaimedEvent, get_or_create_user
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.config import Settings, get_settings
from upravdom.dedup import texts
from upravdom.dedup.callbacks import DedupAction, decode
from upravdom.models import ProblemType
from upravdom.models.enums import TicketStatus
from upravdom.tickets.due import format_due
from upravdom.tickets.notify import status_keyboard
from upravdom.tickets.routing import org_display
from upravdom.tickets.service import get_by_number, split_merged
from upravdom.tickets.texts import format_ticket_number

logger = logging.getLogger(__name__)


async def on_callback(
    session: AsyncSession,
    event: ClaimedEvent,
    *,
    settings: Settings | None = None,
) -> None:
    """`ddp:split:<номер>` — развести склейку в самостоятельную заявку."""

    settings = settings or get_settings()
    parsed = parse_webhook_payload(event.payload)
    if parsed.callback_id and parsed.max_user_id:
        await outbox.enqueue_callback_answer(
            session,
            chat_id=parsed.chat_id or parsed.max_user_id,
            callback_id=parsed.callback_id,
            notification=texts.CALLBACK_ACK,
        )

    cb = decode(parsed.text)
    if cb is None or cb.action is not DedupAction.SPLIT:
        return
    if not parsed.max_user_id or not parsed.chat_id:
        return

    user = await get_or_create_user(session, parsed.max_user_id)
    merged = await get_by_number(session, cb.ticket_number)

    # Номер в кнопке подделывается: разклеить можно только собственную склейку.
    if merged is None or merged.user_id != user.id:
        logger.warning("ddp:split — заявка не принадлежит нажавшему или не найдена")
        return
    # Повторное нажатие: заявка уже самостоятельная — без второго события и ответа.
    if merged.status is not TicketStatus.MERGED:
        return

    ticket = await split_merged(session, merged, actor="resident")

    number = format_ticket_number(ticket.number)
    name, contact = await org_display(session, ticket.routed_to_org_type, ticket.routed_to_org_id)
    phone = contact or texts.SPLIT_NO_CONTACT
    if ticket.status is TicketStatus.NEEDS_DISPATCHER or name is None:
        body = texts.SPLIT_DONE_DISPATCHER.format(number=number, contact=phone)
    else:
        body = texts.SPLIT_DONE.format(number=number, org=name, contact=phone)

    pt = await session.get(ProblemType, ticket.problem_type)
    due = format_due(ticket.due_at, pt.norm_reference if pt else "", settings.display_timezone)
    await outbox.enqueue_message(
        session,
        chat_id=parsed.chat_id,
        text_=f"{body}\n{due}",
        attachments=status_keyboard(ticket.number),
    )


__all__ = ["on_callback"]
