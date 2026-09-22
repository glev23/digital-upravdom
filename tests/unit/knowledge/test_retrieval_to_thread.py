"""Эмбеддинг в search не блокирует event loop (CLASSIFY-001)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from upravdom.knowledge import retrieval


@pytest.mark.asyncio
async def test_search_keeps_event_loop_responsive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пока embed спит в потоке, другая корутина успевает тикнуть."""

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.02)
            ticks += 1

    def slow_embed(texts: list[str], *, prompt: str = "") -> np.ndarray:
        _ = texts, prompt
        time.sleep(0.12)
        return np.zeros((1, 768), dtype=np.float32)

    mock_client = MagicMock()
    mock_client.query_points = AsyncMock(
        return_value=MagicMock(points=[]),
    )

    monkeypatch.setattr(retrieval, "embed", slow_embed)
    monkeypatch.setattr(retrieval, "get_qdrant_client", lambda: mock_client)

    search_task = asyncio.create_task(retrieval.search("тест", k=1, client=mock_client))
    tick_task = asyncio.create_task(ticker())
    await asyncio.gather(search_task, tick_task)

    assert ticks >= 3
    mock_client.query_points.assert_awaited()
