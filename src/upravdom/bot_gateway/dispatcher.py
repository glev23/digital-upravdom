"""Реестр обработчиков по типу события (architecture.md §8).

BOT-001 не содержит бизнес-логики. ONBOARD-001 / FLOW-001 регистрируют
обработчики поверх реестра. Ограничение частоты (architecture.md §10) —
здесь, а не в вебхуке.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import outbox
from upravdom.bot_gateway.inbox import ClaimedEvent, get_or_create_user
from upravdom.bot_gateway.rate_limit import RateLimiter
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.config import get_settings
from upravdom.flow import handlers as flow_handlers
from upravdom.flow.callbacks import PREFIX as FLOW_PREFIX
from upravdom.onboarding import handlers as onboarding_handlers
from upravdom.onboarding.callbacks import PREFIX as ONB_PREFIX

EventHandler = Callable[[AsyncSession, ClaimedEvent], Awaitable[None]]

# Сохранено для тестов BOT-001; живой путь — flow.texts.ACK_RECEIVED.
ACKNOWLEDGEMENT_TEXT = "Принял обращение, определяю ответственного."
RATE_LIMIT_TEXT = "Вы отправляете сообщения слишком часто. Пожалуйста, подождите немного."


async def acknowledge_receipt(session: AsyncSession, event: ClaimedEvent) -> None:
    """Заглушка BOT-001 (тесты). В default_dispatcher заменена на flow.on_message."""

    parsed = parse_webhook_payload(event.payload)
    if not parsed.chat_id:
        return

    await get_or_create_user(session, parsed.max_user_id)
    await outbox.enqueue_message(session, chat_id=parsed.chat_id, text_=ACKNOWLEDGEMENT_TEXT)


async def noop_handler(_session: AsyncSession, _event: ClaimedEvent) -> None:
    """Fallback: системные события без ответа жителю."""


async def route_callback(session: AsyncSession, event: ClaimedEvent) -> None:
    """Маршрутизация кнопок: onb: → онбординг, clf: → основной сценарий."""

    parsed = parse_webhook_payload(event.payload)
    payload = parsed.text or ""
    if payload.startswith(f"{FLOW_PREFIX}:"):
        await flow_handlers.on_callback(session, event)
        return
    if payload.startswith(f"{ONB_PREFIX}:"):
        await onboarding_handlers.on_callback(session, event)
        return
    # Неизвестный payload — всё равно снимем «висящую» кнопку через onboarding
    # (там всегда есть answer_callback).
    await onboarding_handlers.on_callback(session, event)


class Dispatcher:
    """Реестр `event_type -> handler` с обработчиком по умолчанию и лимитом частоты."""

    def __init__(
        self,
        *,
        default_handler: EventHandler = noop_handler,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._handlers: dict[str, EventHandler] = {}
        self._default_handler = default_handler
        self._rate_limiter = rate_limiter or RateLimiter(
            max_per_minute=get_settings().rate_limit_messages_per_minute
        )

    def register(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type] = handler

    async def dispatch(self, session: AsyncSession, event: ClaimedEvent) -> None:
        parsed = parse_webhook_payload(event.payload)

        if parsed.max_user_id and not self._rate_limiter.allow(parsed.max_user_id):
            if parsed.chat_id:
                await outbox.enqueue_message(session, chat_id=parsed.chat_id, text_=RATE_LIMIT_TEXT)
            return

        handler = self._handlers.get(parsed.event_type, self._default_handler)
        await handler(session, event)


default_dispatcher = Dispatcher()
default_dispatcher.register("message_created", onboarding_handlers.gated(flow_handlers.on_message))
default_dispatcher.register("bot_started", onboarding_handlers.on_bot_started)
default_dispatcher.register("message_callback", route_callback)
