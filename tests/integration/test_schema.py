"""Инварианты схемы на реальном PostgreSQL (DB-001).

Каждый тест проверяет, что гарантию обеспечивает **база данных**, а не код
приложения — поэтому нарушения провоцируются сырым SQL через `text()`, а не
через ORM (которая могла бы отклонить значение раньше, на стороне Python,
и тест ничего не доказывал бы про саму схему).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

pytestmark = pytest.mark.asyncio


async def _seed_ticket(conn: AsyncConnection) -> dict[str, uuid.UUID]:
    """Минимальный граф: пользователь → дом → тип проблемы → заявка."""

    user_id, house_id, ticket_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text("INSERT INTO users (id, max_user_id) VALUES (:id, :m)"),
        {"id": user_id, "m": f"u-{user_id}"},
    )
    await conn.execute(
        text(
            "INSERT INTO houses (id, address_raw, region_code) "
            "VALUES (:id, 'ул. Тестовая, 1', 'RU-TA')"
        ),
        {"id": house_id},
    )
    await conn.execute(
        text(
            "INSERT INTO problem_types (code, title, default_responsibility_zone, norm_reference) "
            "VALUES ('cold_water', 'Нет холодной воды', 'uk', 'ПП РФ №491') "
            "ON CONFLICT (code) DO NOTHING"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO tickets (id, user_id, house_id, raw_text, problem_type, "
            "responsibility_zone, confidence, status, created_at, updated_at) "
            "VALUES (:id, :user_id, :house_id, 'test', 'cold_water', 'uk', 0.9, "
            "'accepted', now(), now())"
        ),
        {"id": ticket_id, "user_id": user_id, "house_id": house_id},
    )
    return {"user_id": user_id, "house_id": house_id, "ticket_id": ticket_id}


async def test_inbound_event_idempotency(connection: AsyncConnection) -> None:
    payload = {"id": uuid.uuid4(), "max_event_id": "evt-dup"}
    await connection.execute(
        text(
            "INSERT INTO inbound_events (id, max_event_id, payload, status, attempts, received_at) "
            "VALUES (:id, :max_event_id, '{}', 'pending', 0, now())"
        ),
        payload,
    )
    with pytest.raises(DBAPIError, match="duplicate key"):
        await connection.execute(
            text(
                "INSERT INTO inbound_events "
                "(id, max_event_id, payload, status, attempts, received_at) "
                "VALUES (:id, :max_event_id, '{}', 'pending', 0, now())"
            ),
            {"id": uuid.uuid4(), "max_event_id": "evt-dup"},
        )


async def test_ticket_events_append_only_rejects_update(connection: AsyncConnection) -> None:
    ids = await _seed_ticket(connection)
    await connection.execute(
        text(
            "INSERT INTO ticket_events (id, ticket_id, event_type, to_status, actor, created_at) "
            "VALUES (:id, :ticket_id, 'created', 'accepted', 'system', now())"
        ),
        {"id": uuid.uuid4(), "ticket_id": ids["ticket_id"]},
    )
    with pytest.raises(DBAPIError, match="append-only"):
        await connection.execute(
            text("UPDATE ticket_events SET actor = 'hacker' WHERE ticket_id = :ticket_id"),
            {"ticket_id": ids["ticket_id"]},
        )


async def test_ticket_events_append_only_rejects_delete(connection: AsyncConnection) -> None:
    ids = await _seed_ticket(connection)
    await connection.execute(
        text(
            "INSERT INTO ticket_events (id, ticket_id, event_type, to_status, actor, created_at) "
            "VALUES (:id, :ticket_id, 'created', 'accepted', 'system', now())"
        ),
        {"id": uuid.uuid4(), "ticket_id": ids["ticket_id"]},
    )
    with pytest.raises(DBAPIError, match="append-only"):
        await connection.execute(
            text("DELETE FROM ticket_events WHERE ticket_id = :ticket_id"),
            {"ticket_id": ids["ticket_id"]},
        )


async def test_second_primary_house_rejected(connection: AsyncConnection) -> None:
    user_id, house_a, house_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await connection.execute(
        text("INSERT INTO users (id, max_user_id) VALUES (:id, :m)"),
        {"id": user_id, "m": f"u-{user_id}"},
    )
    for house_id in (house_a, house_b):
        await connection.execute(
            text(
                "INSERT INTO houses (id, address_raw, region_code) VALUES (:id, 'адрес', 'RU-TA')"
            ),
            {"id": house_id},
        )
    await connection.execute(
        text(
            "INSERT INTO user_houses (user_id, house_id, is_primary, created_at) "
            "VALUES (:user_id, :house_id, true, now())"
        ),
        {"user_id": user_id, "house_id": house_a},
    )
    with pytest.raises(DBAPIError, match="duplicate key"):
        await connection.execute(
            text(
                "INSERT INTO user_houses (user_id, house_id, is_primary, created_at) "
                "VALUES (:user_id, :house_id, true, now())"
            ),
            {"user_id": user_id, "house_id": house_b},
        )


async def test_invalid_ticket_status_rejected(connection: AsyncConnection) -> None:
    ids = await _seed_ticket(connection)
    with pytest.raises(DBAPIError, match="ck_tickets_status"):
        await connection.execute(
            text("UPDATE tickets SET status = 'bogus_status' WHERE id = :ticket_id"),
            {"ticket_id": ids["ticket_id"]},
        )


async def test_invalid_responsibility_zone_rejected(connection: AsyncConnection) -> None:
    with pytest.raises(DBAPIError, match="ck_problem_types_default_responsibility_zone"):
        await connection.execute(
            text(
                "INSERT INTO problem_types "
                "(code, title, default_responsibility_zone, norm_reference) "
                "VALUES ('bogus', 'x', 'bogus_zone', 'y')"
            )
        )


async def test_confidence_out_of_range_rejected(connection: AsyncConnection) -> None:
    ids = await _seed_ticket(connection)
    with pytest.raises(DBAPIError, match="ck_tickets_confidence_range"):
        await connection.execute(
            text(
                "INSERT INTO tickets (id, user_id, house_id, raw_text, problem_type, "
                "responsibility_zone, confidence, status, created_at, updated_at) "
                "VALUES (:id, :user_id, :house_id, 'test2', 'cold_water', 'uk', 1.5, "
                "'accepted', now(), now())"
            ),
            {"id": uuid.uuid4(), "user_id": ids["user_id"], "house_id": ids["house_id"]},
        )


async def test_organization_requires_explicit_is_test_data(connection: AsyncConnection) -> None:
    with pytest.raises(DBAPIError, match="not-null"):
        await connection.execute(
            text(
                "INSERT INTO management_companies (id, name, region_code) "
                "VALUES (:id, 'УК без флага', 'RU-TA')"
            ),
            {"id": uuid.uuid4()},
        )
