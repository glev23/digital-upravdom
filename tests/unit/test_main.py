"""Тесты `/health` и `/ready` без обращения к реальным хранилищам.

Готовность хранилищ подменяется через monkeypatch — эти тесты проверяют
поведение эндпоинтов (коды ответа, состав тела), а не сами хранилища.
Реальное подключение проверяется вручную в рамках критериев приёмки INIT-001
(`docker compose up` + `curl /ready`), а на PostgreSQL/Qdrant — задачами,
которые их используют (DB-001, KB-001).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from upravdom.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health_returns_ok_without_touching_storage(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("/health не должен обращаться к хранилищам")

    monkeypatch.setattr("upravdom.main.check_database_ready", fail)
    monkeypatch.setattr("upravdom.main.check_qdrant_ready", fail)

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_ok_when_both_storages_up(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr("upravdom.main.check_database_ready", ok)
    monkeypatch.setattr("upravdom.main.check_qdrant_ready", ok)

    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "postgres": True, "qdrant": True}


async def test_ready_503_when_qdrant_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(*_args: object, **_kwargs: object) -> bool:
        return True

    async def down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr("upravdom.main.check_database_ready", ok)
    monkeypatch.setattr("upravdom.main.check_qdrant_ready", down)

    response = await client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["postgres"] is True
    assert body["qdrant"] is False


async def test_ready_503_when_postgres_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def ok(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr("upravdom.main.check_database_ready", down)
    monkeypatch.setattr("upravdom.main.check_qdrant_ready", ok)

    response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["postgres"] is False
