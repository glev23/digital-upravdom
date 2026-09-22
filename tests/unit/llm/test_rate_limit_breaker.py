"""Локальный лимит и размыкатель OpenRouterClient (время подменяется)."""

from __future__ import annotations

from typing import cast

import httpx
import pytest
from pydantic import BaseModel

from upravdom.classifier.llm.openrouter import OpenRouterClient
from upravdom.classifier.llm.port import ChatMessage, LlmRateLimited, LlmUnavailable
from upravdom.config import Settings


class _Tiny(BaseModel):
    label: str


MESSAGES = [ChatMessage(role="user", content="x")]


def _settings(**overrides: object) -> Settings:
    data = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "qdrant_url": "http://localhost:6333",
        "openrouter_api_key": "sk-test-secret-key",
        "openrouter_model": "test/primary:free",
        "openrouter_model_fallback": "test/fallback:free",
        "llm_timeout_seconds": 8.0,
        "llm_max_requests_per_minute": 100,
        "llm_circuit_failure_threshold": 3,
        "llm_circuit_open_seconds": 60.0,
        **overrides,
    }
    return Settings(_env_file=None, **data)  # type: ignore[call-arg,arg-type]


def _ok_body(*, content: str, model: str = "test/primary:free") -> dict[str, object]:
    return {
        "id": "gen-1",
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.asyncio
async def test_local_rate_limit_immediate() -> None:
    clock = {"t": 1000.0}

    def now() -> float:
        return clock["t"]

    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=_ok_body(content='{"label":"a"}'))
    )
    client = OpenRouterClient(
        settings=_settings(llm_max_requests_per_minute=2),
        transport=transport,
        time_fn=now,
    )
    await client.complete_json(MESSAGES, schema=_Tiny)
    await client.complete_json(MESSAGES, schema=_Tiny)
    with pytest.raises(LlmRateLimited, match="local"):
        await client.complete_json(MESSAGES, schema=_Tiny)


@pytest.mark.asyncio
async def test_circuit_opens_and_half_opens() -> None:
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    def fail(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "down"}})

    client = OpenRouterClient(
        settings=_settings(
            openrouter_model_fallback=None,
            llm_circuit_failure_threshold=3,
            llm_circuit_open_seconds=60,
            llm_max_requests_per_minute=100,
        ),
        transport=httpx.MockTransport(fail),
        time_fn=now,
    )
    for _ in range(3):
        with pytest.raises(LlmUnavailable):
            await client.complete_json(MESSAGES, schema=_Tiny)
        clock["t"] += 1.0

    with pytest.raises(LlmUnavailable, match="circuit"):
        await client.complete_json(MESSAGES, schema=_Tiny)

    clock["t"] += 60.0
    with pytest.raises(LlmUnavailable):
        await client.complete_json(MESSAGES, schema=_Tiny)


@pytest.mark.asyncio
async def test_circuit_closes_after_success() -> None:
    clock = {"t": 0.0}
    state = {"n": 0}

    def now() -> float:
        return clock["t"]

    def flaky(request: httpx.Request) -> httpx.Response:
        _ = request
        state["n"] += 1
        if state["n"] <= 3:
            return httpx.Response(503, json={"error": {"message": "down"}})
        return httpx.Response(200, json=_ok_body(content='{"label":"ok"}'))

    client = OpenRouterClient(
        settings=_settings(
            openrouter_model_fallback=None,
            llm_circuit_failure_threshold=3,
            llm_circuit_open_seconds=10,
            llm_max_requests_per_minute=100,
        ),
        transport=httpx.MockTransport(flaky),
        time_fn=now,
    )
    for _ in range(3):
        with pytest.raises(LlmUnavailable):
            await client.complete_json(MESSAGES, schema=_Tiny)
        clock["t"] += 1

    clock["t"] += 10
    result = await client.complete_json(MESSAGES, schema=_Tiny)
    assert cast(_Tiny, result.data).label == "ok"
    result2 = await client.complete_json(MESSAGES, schema=_Tiny)
    assert cast(_Tiny, result2.data).label == "ok"


@pytest.mark.asyncio
async def test_dead_primary_is_skipped_once_its_breaker_opens() -> None:
    """Успех резервной модели не должен обнулять сбои основной (иначе каждое
    сообщение ждёт полный таймаут пропавшей бесплатной модели)."""

    import json

    clock = {"t": 0.0}
    calls: list[str] = []

    def now() -> float:
        return clock["t"]

    def route(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "test/primary:free":
            return httpx.Response(503, json={"error": {"message": "gone"}})
        return httpx.Response(
            200, json=_ok_body(content='{"label":"fb"}', model="test/fallback:free")
        )

    client = OpenRouterClient(
        settings=_settings(llm_circuit_failure_threshold=3, llm_circuit_open_seconds=60),
        transport=httpx.MockTransport(route),
        time_fn=now,
    )
    for _ in range(3):
        result = await client.complete_json(MESSAGES, schema=_Tiny)
        assert result.fallback_model_used
        clock["t"] += 1.0

    calls.clear()
    result = await client.complete_json(MESSAGES, schema=_Tiny)

    assert result.fallback_model_used
    assert calls == ["test/fallback:free"]  # основная не вызывалась

    clock["t"] += 60.0  # после паузы основная снова пробуется
    calls.clear()
    await client.complete_json(MESSAGES, schema=_Tiny)
    assert calls[0] == "test/primary:free"


def test_process_wide_client_is_shared() -> None:
    """Лимит и размыкатель живут в экземпляре — клиент должен быть один на процесс."""

    from upravdom.classifier.llm import get_llm_client

    get_llm_client.cache_clear()
    try:
        assert get_llm_client() is get_llm_client()
    finally:
        get_llm_client.cache_clear()
