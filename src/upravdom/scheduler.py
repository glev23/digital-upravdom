"""Воркер inbox/outbox в процессе приложения (architecture.md §3, §16 — шаг 1
лестницы масштабирования: MVP не выносит планировщик в отдельный процесс).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from upravdom.bot_gateway import inbox, outbox
from upravdom.bot_gateway.dispatcher import Dispatcher, default_dispatcher
from upravdom.bot_gateway.max_client import HttpxMaxClient, MaxClient
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.config import get_settings
from upravdom.db import session_scope
from upravdom.models import Notification
from upravdom.notifications import service as notifications_service

logger = logging.getLogger(__name__)

FAILURE_TEXT = (
    "Произошёл сбой при обработке обращения. "
    "Пожалуйста, обратитесь в аварийно-диспетчерскую службу вашей УК напрямую."
)


def build_max_client() -> MaxClient | None:
    """None, если токен/адрес API не настроены — воркер отправки просто ждёт.

    Приложение обязано подниматься без внешних токенов (INIT-001); молчаливо
    падать в фоне из-за отсутствующей конфигурации оно не должно.
    """

    settings = get_settings()
    if not settings.max_bot_token or not settings.max_api_base_url:
        return None
    return HttpxMaxClient(base_url=settings.max_api_base_url, token=settings.max_bot_token)


async def process_inbound_batch(*, dispatcher: Dispatcher = default_dispatcher) -> None:
    settings = get_settings()

    async with session_scope() as session:
        await inbox.reclaim_stale_processing(
            session,
            visibility_timeout=timedelta(seconds=settings.inbound_visibility_timeout_seconds),
            max_attempts=settings.inbound_max_attempts,
        )

    async with session_scope() as session:
        claimed = await inbox.claim_batch(session, batch_size=settings.worker_batch_size)

    for event in claimed:
        async with session_scope() as session:
            try:
                await dispatcher.dispatch(session, event)
            except Exception as exc:  # noqa: BLE001 — обработчик может бросить что угодно
                # Откат частичных эффектов обработчика (привязка без согласия,
                # поставленный в очередь ответ) — они не должны пережить сбой.
                await session.rollback()
                # Только тип исключения — текст сообщения жителя мог попасть
                # в аргументы исключения обработчика (architecture.md §11).
                logger.warning("handler failed for event %s: %s", event.id, type(exc).__name__)
                is_final = await inbox.mark_failed_attempt(
                    session,
                    event.id,
                    attempts=event.attempts,
                    max_attempts=settings.inbound_max_attempts,
                    error=type(exc).__name__,
                )
                if is_final:
                    parsed = parse_webhook_payload(event.payload)
                    if parsed.chat_id:
                        await outbox.enqueue_message(
                            session, chat_id=parsed.chat_id, text_=FAILURE_TEXT
                        )
                        await session.commit()
                continue
            await inbox.mark_done(session, event.id)


async def process_notifications() -> None:
    """Рассылка уведомлений об отключениях (NOTIFY-001).

    Одна транзакция на уведомление: сбой на одном доме не должен блокировать
    остальные. Сама отправка — через `outbound_messages` с повторами, своего
    механизма повторов здесь нет.
    """

    settings = get_settings()
    async with session_scope() as session:
        active = await notifications_service.list_active(session)

    for notification in active:
        async with session_scope() as session:
            try:
                fresh = await session.get(Notification, notification.id)
                if fresh is None:
                    continue
                queued = await notifications_service.dispatch(session, fresh, settings=settings)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 — одно уведомление не валит проход
                await session.rollback()
                logger.warning(
                    "notify: уведомление %s не разослано: %s",
                    notification.id,
                    type(exc).__name__,
                )
                continue
            if queued:
                logger.info("notify: уведомление %s — %d доставок", notification.id, queued)


async def process_outbound_batch(*, client: MaxClient | None) -> None:
    if client is None:
        return

    settings = get_settings()
    async with session_scope() as session:
        claimed = await outbox.claim_batch(session, batch_size=settings.worker_batch_size)

    for message in claimed:
        async with session_scope() as session:
            await outbox.send_claimed(
                session,
                message,
                client=client,
                max_attempts=settings.outbound_max_attempts,
                backoff_base_seconds=settings.outbound_backoff_base_seconds,
            )


def create_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    client = build_max_client()
    if client is None:
        logger.warning(
            "MAX_BOT_TOKEN/MAX_API_BASE_URL не заданы — outbound-воркер запущен, "
            "но отправлять исходящие не будет (сообщения останутся в очереди)"
        )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        process_inbound_batch,
        "interval",
        seconds=settings.worker_poll_interval_seconds,
        id="inbox_worker",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        process_outbound_batch,
        "interval",
        seconds=settings.worker_poll_interval_seconds,
        id="outbox_worker",
        max_instances=1,
        coalesce=True,
        kwargs={"client": client},
    )
    scheduler.add_job(
        process_notifications,
        "interval",
        seconds=settings.notify_poll_interval_seconds,
        id="notify_worker",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
