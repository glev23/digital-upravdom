"""Склейка массовых обращений об одной аварии (DEDUP-001, architecture.md §6.5).

Запускается после классификации и **до** создания заявки. Ни один отказ здесь
не блокирует основной сценарий: не посчитался вектор, упал поиск кандидата —
заявка создаётся обычным путём, в лог уходит только тип исключения (§11).

Кандидаты ищутся среди заявок этого дома напрямую в Postgres, без Qdrant:
выборка мала, а недоступность векторного хранилища не должна ломать склейку
(§6.5, §9).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.config import Settings, get_settings
from upravdom.dedup.similarity import cosine, embed_for_dedup
from upravdom.masking import mask
from upravdom.models import Ticket, TicketEvent, TicketSubscriber
from upravdom.models.enums import JoinReason, TicketEventType, TicketStatus

logger = logging.getLogger(__name__)

# Головная заявка должна быть ещё открыта: закрытая не собирает новые обращения.
_OPEN_STATUSES = (
    TicketStatus.ACCEPTED,
    TicketStatus.NEEDS_DISPATCHER,
    TicketStatus.IN_PROGRESS,
    TicketStatus.ROUTED_TO_CONTRACTOR,
)

# Для `other` совпадение типа ничего не говорит о том, что это одна авария.
NON_MERGEABLE_PROBLEM_TYPES = frozenset({"other"})


@dataclass(slots=True, frozen=True)
class MergeCandidate:
    head: Ticket
    similarity: float
    author_already_subscribed: bool


async def embed_message(raw_text: str) -> list[float] | None:
    """Вектор маскированного текста; None — сбой (заявка создаётся обычным путём).

    `embed()` синхронный и тяжёлый — только через `asyncio.to_thread`, иначе
    встаёт весь event loop (вебхук, воркеры).
    """

    try:
        masked = str(mask(raw_text).text)
        return await asyncio.to_thread(partial(embed_for_dedup, masked))
    except Exception as exc:  # noqa: BLE001 — сбой эмбеддинга не ломает сценарий
        logger.warning("dedup: эмбеддинг не посчитан (%s)", type(exc).__name__)
        return None


async def find_candidate(
    session: AsyncSession,
    *,
    house_id: uuid.UUID,
    problem_type: str,
    author_id: uuid.UUID,
    embedding: list[float] | None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> MergeCandidate | None:
    """Самый похожий открытый кандидат этого дома, если выполнены все условия."""

    if embedding is None or problem_type in NON_MERGEABLE_PROBLEM_TYPES:
        return None

    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    window_start = now - timedelta(hours=settings.dedup_window_hours)

    try:
        rows = (
            (
                await session.execute(
                    select(Ticket).where(
                        Ticket.house_id == house_id,
                        Ticket.problem_type == problem_type,
                        Ticket.status.in_(_OPEN_STATUSES),
                        # Цепочек склеек нет: дубль не может быть головной.
                        Ticket.duplicate_of_ticket_id.is_(None),
                        Ticket.created_at >= window_start,
                        Ticket.text_embedding.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedup: поиск кандидатов не удался (%s)", type(exc).__name__)
        return None

    best: MergeCandidate | None = None
    for head in rows:
        score = cosine(embedding, head.text_embedding or [])
        if score < settings.dedup_similarity_threshold:
            continue
        if best is None or score > best.similarity:
            best = MergeCandidate(head=head, similarity=score, author_already_subscribed=False)

    if best is None:
        return None

    subscription = await session.get(TicketSubscriber, (best.head.id, author_id))
    return MergeCandidate(
        head=best.head,
        similarity=best.similarity,
        author_already_subscribed=subscription is not None,
    )


async def subscribe_to_head(
    session: AsyncSession,
    *,
    head: Ticket,
    user_id: uuid.UUID,
    merged_number: int,
    now: datetime | None = None,
) -> None:
    """Подписка на головную + событие о присоединении.

    В `payload` — номер склеенного обращения и текущее число подписчиков:
    диспетчер видит реальный масштаб аварии (сколько квартир сообщило), а не
    одну строку (architecture.md §6.5).
    """

    now = now or datetime.now(UTC)
    session.add(
        TicketSubscriber(
            ticket_id=head.id,
            user_id=user_id,
            is_author=False,
            joined_at=now,
            join_reason=JoinReason.DEDUP_AUTO,
        )
    )
    await session.flush()

    subscribers = len(
        (
            await session.execute(
                select(TicketSubscriber.user_id).where(TicketSubscriber.ticket_id == head.id)
            )
        )
        .scalars()
        .all()
    )
    session.add(
        TicketEvent(
            id=uuid.uuid4(),
            ticket_id=head.id,
            event_type=TicketEventType.SUBSCRIBER_JOINED.value,
            from_status=None,
            to_status=head.status,
            actor="system",
            payload={"merged_ticket_number": merged_number, "subscribers_count": subscribers},
            created_at=now,
        )
    )
    await session.flush()
