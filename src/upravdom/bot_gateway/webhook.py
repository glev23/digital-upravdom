"""`POST /webhook/max` — единственная точка входа для событий MAX (architecture.md §7).

Порядок обязателен и проверяется тестами: подлинность → запись в БД → ответ.
Ответ уходит до какой-либо обработки события — обработка асинхронная,
через воркер (architecture.md §3).
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway.inbox import record_inbound_event
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.config import get_settings
from upravdom.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter()

# Подтверждено MAX-001 (documentation/max_api.md §4): секрет, заданный при
# создании подписки (POST /subscriptions), MAX возвращает В НЕИЗМЕНЁННОМ
# ВИДЕ в этом заголовке — не HMAC-подпись тела запроса. Проверка — прямое
# сравнение строк (константное время), не пересчёт хэша.
SIGNATURE_HEADER = "X-Max-Bot-Api-Secret"


def verify_signature(raw_body: bytes, headers: Mapping[str, str], *, secret: str | None) -> bool:
    """Fail-closed: без настроенного секрета — отказ, а не пропуск проверки.

    `raw_body` не используется в сравнении (MAX не подписывает тело) —
    параметр сохранён в сигнатуре ради единообразия с общим шаблоном
    проверки вебхуков и на случай, если платформа впредь добавит
    подпись тела отдельно от `secret`.
    """

    if not secret:
        return False
    provided = headers.get(SIGNATURE_HEADER)
    if not provided:
        return False
    return hmac.compare_digest(provided, secret)


@router.post("/webhook/max", status_code=status.HTTP_200_OK)
async def receive_max_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 — стандартная идиома FastAPI DI
) -> Response:
    raw_body = await request.body()
    settings = get_settings()

    if not verify_signature(raw_body, request.headers, secret=settings.max_webhook_secret):
        # Логируем факт отказа, не заголовки и не тело — там может быть
        # чувствительное содержимое сообщения жителя.
        logger.warning("webhook signature rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json") from exc

    parsed = parse_webhook_payload(payload)

    try:
        await record_inbound_event(
            session,
            max_event_id=parsed.event_id,
            payload=payload,
            max_user_id=parsed.max_user_id,
        )
    except SQLAlchemyError as exc:
        logger.error("inbound event not persisted: storage unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage unavailable"
        ) from exc

    return Response(status_code=status.HTTP_200_OK)
