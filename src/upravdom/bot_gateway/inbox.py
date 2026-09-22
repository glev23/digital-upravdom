"""Идемпотентный inbox и захват событий (architecture.md §3).

Идемпотентность обеспечивает уникальный индекс `inbound_events.max_event_id`
в БД (DB-001), не код: `record_inbound_event` полагается на
`ON CONFLICT ... DO NOTHING`, а не на предварительную проверку "есть ли
уже такая запись" — иначе между проверкой и вставкой возможна гонка.

Захват — одним атомарным `UPDATE ... FROM (SELECT ... FOR UPDATE SKIP
LOCKED)`, чтобы два конкурентных воркера не забрали одно и то же событие
(architecture.md §3, критерий приёмки BOT-001).
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.models import InboundEvent, User
from upravdom.models.enums import InboundEventStatus


@dataclasses.dataclass(slots=True, frozen=True)
class ClaimedEvent:
    """То немногое, что нужно диспетчеру: не вся ORM-строка, только данные."""

    id: uuid.UUID
    max_event_id: str
    payload: dict[str, object]
    attempts: int


async def record_inbound_event(
    session: AsyncSession,
    *,
    max_event_id: str,
    payload: dict[str, object],
    max_user_id: str | None = None,
) -> None:
    """`INSERT ... ON CONFLICT DO NOTHING` — повтор доставки не создаёт строку."""

    stmt = (
        pg_insert(InboundEvent)
        .values(
            id=uuid.uuid4(),
            max_event_id=max_event_id,
            max_user_id=max_user_id or None,
            payload=payload,
            status=InboundEventStatus.PENDING,
            attempts=0,
            received_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["max_event_id"])
    )
    await session.execute(stmt)
    await session.commit()


async def reclaim_stale_processing(
    session: AsyncSession, *, visibility_timeout: timedelta, max_attempts: int
) -> None:
    """Событие в `processing` дольше таймаута — воркер, скорее всего, упал.

    Две ветки: ещё есть попытки — назад в `pending`; исчерпаны — в `failed`
    (architecture.md §3, §10: тишина недопустима, но и бесконечный повтор тоже).
    """

    cutoff = datetime.now(UTC) - visibility_timeout
    await session.execute(
        text(
            "UPDATE inbound_events SET status = 'pending', processing_started_at = NULL "
            "WHERE status = 'processing' AND processing_started_at < :cutoff "
            "AND attempts < :max_attempts"
        ),
        {"cutoff": cutoff, "max_attempts": max_attempts},
    )
    await session.execute(
        text(
            "UPDATE inbound_events "
            "SET status = 'failed', processed_at = now(), last_error = 'stale: worker restarted' "
            "WHERE status = 'processing' AND processing_started_at < :cutoff "
            "AND attempts >= :max_attempts"
        ),
        {"cutoff": cutoff, "max_attempts": max_attempts},
    )
    await session.commit()


async def claim_batch(session: AsyncSession, *, batch_size: int) -> list[ClaimedEvent]:
    """Атомарный захват: `FOR UPDATE SKIP LOCKED` + `UPDATE ... RETURNING`."""

    result = await session.execute(
        text(
            """
            WITH claimed AS (
                SELECT id FROM inbound_events
                WHERE status = 'pending'
                ORDER BY received_at
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            UPDATE inbound_events
            SET status = 'processing', attempts = attempts + 1, processing_started_at = now()
            FROM claimed
            WHERE inbound_events.id = claimed.id
            RETURNING inbound_events.id, inbound_events.max_event_id,
                      inbound_events.payload, inbound_events.attempts
            """
        ),
        {"batch_size": batch_size},
    )
    await session.commit()
    return [
        ClaimedEvent(
            id=row.id,
            max_event_id=row.max_event_id,
            payload=row.payload,
            attempts=row.attempts,
        )
        for row in result
    ]


async def mark_done(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Фиксирует и эффекты обработчика, и `done` — одним commit.

    Условие `status = 'processing'`: обработчик мог сам перевести событие в
    `awaiting_consent` (ONBOARD-001) — перетирать это в `done` нельзя.
    """

    await session.execute(
        text(
            "UPDATE inbound_events SET status = 'done', processed_at = now() "
            "WHERE id = :id AND status = 'processing'"
        ),
        {"id": event_id},
    )
    await session.commit()


async def hold_for_consent(session: AsyncSession, event_id: uuid.UUID) -> None:
    """Удерживает событие до согласия на ПДн (architecture.md §7.1). Без commit."""

    await session.execute(
        text(
            "UPDATE inbound_events SET status = 'awaiting_consent', processing_started_at = NULL "
            "WHERE id = :id"
        ),
        {"id": event_id},
    )


async def release_held(session: AsyncSession, max_user_id: str) -> int:
    """После согласия удержанные события жителя снова идут в обработку. Без commit.

    `attempts` обнуляется: удержание — не неудачная попытка, и оно не
    должно съедать лимит повторов реальной обработки.
    """

    result = await session.execute(
        text(
            "UPDATE inbound_events SET status = 'pending', attempts = 0 "
            "WHERE status = 'awaiting_consent' AND max_user_id = :u"
        ),
        {"u": max_user_id},
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def close_held(session: AsyncSession, max_user_id: str, *, reason: str) -> int:
    """Отказ от согласия: удержанное закрывается без обработки. Без commit."""

    result = await session.execute(
        text(
            "UPDATE inbound_events SET status = 'done', processed_at = now(), last_error = :r "
            "WHERE status = 'awaiting_consent' AND max_user_id = :u"
        ),
        {"u": max_user_id, "r": reason},
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def mark_failed_attempt(
    session: AsyncSession, event_id: uuid.UUID, *, attempts: int, max_attempts: int, error: str
) -> bool:
    """Возвращает True, если это была финальная неудача (`failed`).

    `error` — уже санитизированное сообщение (без текста жителя, см.
    architecture.md §11); вызывающий код отвечает за маскирование.
    """

    if attempts >= max_attempts:
        await session.execute(
            text(
                "UPDATE inbound_events "
                "SET status = 'failed', processed_at = now(), last_error = :error "
                "WHERE id = :id"
            ),
            {"id": event_id, "error": error},
        )
        await session.commit()
        return True

    await session.execute(
        text(
            "UPDATE inbound_events "
            "SET status = 'pending', processing_started_at = NULL, last_error = :error "
            "WHERE id = :id"
        ),
        {"id": event_id, "error": error},
    )
    await session.commit()
    return False


async def get_or_create_user(session: AsyncSession, max_user_id: str) -> User:
    """Первое обращение от нового пользователя создаёт запись `users`."""

    stmt = (
        pg_insert(User)
        .values(id=uuid.uuid4(), max_user_id=max_user_id)
        .on_conflict_do_nothing(index_elements=["max_user_id"])
    )
    await session.execute(stmt)
    result = await session.execute(
        text("SELECT id, max_user_id, created_at FROM users WHERE max_user_id = :m"),
        {"m": max_user_id},
    )
    row = result.one()
    return User(id=row.id, max_user_id=row.max_user_id, created_at=row.created_at)
