"""Подключение к Qdrant.

INIT-001 создаёт только клиент и проверку доступности для `/ready`.
Коллекции (`kb_chunks`, `semantic_cache`) создаёт KB-001 (architecture.md §6.1).
"""

from __future__ import annotations

from functools import lru_cache

from qdrant_client import AsyncQdrantClient

from upravdom.config import get_settings


@lru_cache
def get_qdrant_client() -> AsyncQdrantClient:
    settings = get_settings()
    return AsyncQdrantClient(url=settings.qdrant_url)


async def check_qdrant_ready() -> bool:
    """Возвращает True, если Qdrant отвечает на запрос списка коллекций."""

    try:
        client = get_qdrant_client()
        await client.get_collections()
        return True
    except Exception:  # noqa: BLE001 — любой сбой хранилища означает "не готов"
        return False
