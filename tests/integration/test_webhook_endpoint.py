"""`POST /webhook/max` целиком: подпись, идемпотентность, 503 (BOT-001).

Через реальный HTTP-стек (httpx + ASGITransport) поверх реальной БД —
не мок сессии, чтобы проверять именно то, что увидит MAX. Подпись и
payload — по подтверждённой MAX-001 схеме (max_api.md §4, §5): секрет в
заголовке `X-Max-Bot-Api-Secret` как есть, идемпотентность через
`message.body.mid`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from upravdom.bot_gateway.webhook import SIGNATURE_HEADER
from upravdom.config import get_settings
from upravdom.main import create_app

pytestmark = pytest.mark.asyncio

SECRET = "test-webhook-secret"  # noqa: S105


def _message_payload(mid: str) -> bytes:
    return json.dumps(
        {
            "update_type": "message_created",
            "timestamp": 1700000000000,
            "chat_id": "1",
            "message": {
                "sender": {"user_id": "1"},
                "recipient": {"chat_id": "1"},
                "body": {"mid": mid, "text": "x"},
            },
        }
    ).encode()


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    get_settings.cache_clear()


async def _cleanup_event(max_event_id: str) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM inbound_events WHERE max_event_id = :m"), {"m": max_event_id}
            )
    finally:
        await engine.dispose()


async def test_valid_signature_returns_200(client: AsyncClient) -> None:
    mid = f"mid-{uuid.uuid4()}"
    body = _message_payload(mid)

    try:
        response = await client.post(
            "/webhook/max", content=body, headers={SIGNATURE_HEADER: SECRET}
        )
        assert response.status_code == 200
    finally:
        await _cleanup_event(mid)


async def test_invalid_signature_returns_401_and_no_row(client: AsyncClient) -> None:
    mid = f"mid-{uuid.uuid4()}"
    body = _message_payload(mid)

    response = await client.post(
        "/webhook/max", content=body, headers={SIGNATURE_HEADER: "wrong-secret"}
    )

    assert response.status_code == 401

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT count(*) FROM inbound_events WHERE max_event_id = :m"),
                {"m": mid},
            )
            assert result.scalar_one() == 0
    finally:
        await engine.dispose()


async def test_triple_delivery_creates_one_row(client: AsyncClient) -> None:
    mid = f"mid-{uuid.uuid4()}"
    body = _message_payload(mid)
    headers = {SIGNATURE_HEADER: SECRET}

    try:
        for _ in range(3):
            response = await client.post("/webhook/max", content=body, headers=headers)
            assert response.status_code == 200

        settings = get_settings()
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT count(*) FROM inbound_events WHERE max_event_id = :m"),
                    {"m": mid},
                )
                assert result.scalar_one() == 1
        finally:
            await engine.dispose()
    finally:
        await _cleanup_event(mid)


async def test_storage_unavailable_returns_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Критерий приёмки: при недоступном PostgreSQL вебхук отвечает 503.

    Реальную остановку контейнера в юнит-тесте не делаем (медленно и
    хрупко для CI) — эмулируем сбой хранилища на границе, которую и
    проверяет этот критерий: `record_inbound_event` бросает исключение
    хранилища, а не то, что конкретно Postgres выключен физически.
    """

    from sqlalchemy.exc import OperationalError

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("statement", {}, Exception("connection refused"))

    monkeypatch.setattr("upravdom.bot_gateway.webhook.record_inbound_event", broken)

    body = _message_payload(f"mid-{uuid.uuid4()}")
    response = await client.post("/webhook/max", content=body, headers={SIGNATURE_HEADER: SECRET})

    assert response.status_code == 503
