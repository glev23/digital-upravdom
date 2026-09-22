"""Ветки OpenRouterClient на httpx.MockTransport (без сети)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import httpx
import pytest
from pydantic import BaseModel, Field

from upravdom.classifier.llm.openrouter import OpenRouterClient
from upravdom.classifier.llm.port import (
    ChatMessage,
    LlmBadResponse,
    LlmNotConfigured,
    LlmRateLimited,
    LlmUnavailable,
)
from upravdom.config import Settings


class _Tiny(BaseModel):
    label: str = Field(description="метка")


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


def _transport(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    routes: dict[str, httpx.Response] | None = None,
) -> httpx.MockTransport:
    route_map = routes or {}

    def default(request: httpx.Request) -> httpx.Response:
        assert "sk-test-secret-key" in request.headers.get("Authorization", "")
        assert request.headers["Authorization"].startswith("Bearer ")
        body = json.loads(request.content.decode())
        assert body["temperature"] == 0
        assert "response_format" in body
        if handler is not None:
            return handler(request)
        model = body.get("model")
        if model in route_map:
            return route_map[model]
        raise AssertionError(f"unexpected model {model}")

    if handler is None and not route_map:
        return httpx.MockTransport(lambda _r: httpx.Response(500, json={"error": {"message": "x"}}))
    return httpx.MockTransport(default)


MESSAGES = [ChatMessage(role="user", content="тест")]


@pytest.mark.asyncio
async def test_success() -> None:
    transport = _transport(
        routes={"test/primary:free": httpx.Response(200, json=_ok_body(content='{"label":"ok"}'))}
    )
    client = OpenRouterClient(settings=_settings(), transport=transport)
    result = await client.complete_json(MESSAGES, schema=_Tiny)
    assert cast(_Tiny, result.data).label == "ok"
    assert result.model_name == "test/primary:free"
    assert result.fallback_model_used is False
    assert result.usage and result.usage.total_tokens == 15


@pytest.mark.asyncio
async def test_json_fence_wrapper() -> None:
    content = '```json\n{"label":"fenced"}\n```'
    transport = _transport(
        routes={"test/primary:free": httpx.Response(200, json=_ok_body(content=content))}
    )
    client = OpenRouterClient(settings=_settings(), transport=transport)
    result = await client.complete_json(MESSAGES, schema=_Tiny)
    assert cast(_Tiny, result.data).label == "fenced"


@pytest.mark.asyncio
async def test_invalid_json_then_fallback_ok() -> None:
    transport = _transport(
        routes={
            "test/primary:free": httpx.Response(200, json=_ok_body(content="not-json")),
            "test/fallback:free": httpx.Response(
                200, json=_ok_body(content='{"label":"fb"}', model="test/fallback:free")
            ),
        }
    )
    client = OpenRouterClient(settings=_settings(), transport=transport)
    result = await client.complete_json(MESSAGES, schema=_Tiny)
    assert cast(_Tiny, result.data).label == "fb"
    assert result.fallback_model_used is True
    assert result.model_name == "test/fallback:free"


@pytest.mark.asyncio
async def test_schema_mismatch() -> None:
    transport = _transport(
        routes={
            "test/primary:free": httpx.Response(200, json=_ok_body(content='{"other":1}')),
            "test/fallback:free": httpx.Response(
                200, json=_ok_body(content='{"other":2}', model="test/fallback:free")
            ),
        }
    )
    client = OpenRouterClient(settings=_settings(), transport=transport)
    with pytest.raises(LlmBadResponse):
        await client.complete_json(MESSAGES, schema=_Tiny)


@pytest.mark.asyncio
async def test_timeout() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = OpenRouterClient(
        settings=_settings(openrouter_model_fallback=None),
        transport=httpx.MockTransport(boom),
    )
    with pytest.raises(LlmUnavailable, match="timeout"):
        await client.complete_json(MESSAGES, schema=_Tiny)


@pytest.mark.asyncio
async def test_5xx() -> None:
    transport = _transport(
        routes={
            "test/primary:free": httpx.Response(503, json={"error": {"message": "down"}}),
            "test/fallback:free": httpx.Response(503, json={"error": {"message": "down"}}),
        }
    )
    client = OpenRouterClient(settings=_settings(), transport=transport)
    with pytest.raises(LlmUnavailable):
        await client.complete_json(MESSAGES, schema=_Tiny)


@pytest.mark.asyncio
async def test_429() -> None:
    transport = _transport(
        routes={
            "test/primary:free": httpx.Response(429, json={"error": {"code": 429}}),
            "test/fallback:free": httpx.Response(429, json={"error": {"code": 429}}),
        }
    )
    client = OpenRouterClient(settings=_settings(), transport=transport)
    with pytest.raises(LlmRateLimited):
        await client.complete_json(MESSAGES, schema=_Tiny)


@pytest.mark.asyncio
async def test_error_in_body_with_http_200() -> None:
    transport = _transport(
        routes={
            "test/primary:free": httpx.Response(
                200, json={"error": {"code": 500, "message": "upstream"}}
            ),
            "test/fallback:free": httpx.Response(
                200, json={"error": {"code": 500, "message": "upstream"}}
            ),
        }
    )
    client = OpenRouterClient(settings=_settings(), transport=transport)
    with pytest.raises(LlmUnavailable):
        await client.complete_json(MESSAGES, schema=_Tiny)


@pytest.mark.asyncio
async def test_not_configured() -> None:
    client = OpenRouterClient(
        settings=_settings(openrouter_api_key=None, openrouter_model=None),
        transport=_transport(),
    )
    with pytest.raises(LlmNotConfigured):
        await client.complete_json(MESSAGES, schema=_Tiny)


@pytest.mark.asyncio
async def test_secret_not_in_repr_or_exception() -> None:
    client = OpenRouterClient(settings=_settings(), transport=_transport())
    assert "sk-test-secret-key" not in repr(client)
    with pytest.raises(Exception) as ei:
        await client.complete_json(MESSAGES, schema=_Tiny)
    assert "sk-test-secret-key" not in str(ei.value)
    assert "sk-test-secret-key" not in repr(ei.value)


@pytest.mark.asyncio
async def test_budget_at_most_two_timeouts() -> None:
    calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        calls.append(1.0)
        raise httpx.ReadTimeout("slow")

    client = OpenRouterClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LlmUnavailable):
        await client.complete_json(MESSAGES, schema=_Tiny, timeout_s=8.0)
    assert len(calls) == 2
