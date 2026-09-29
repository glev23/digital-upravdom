"""Ветки OpenRouterClient на httpx.MockTransport (без сети)."""

from __future__ import annotations

import asyncio
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
from upravdom.classifier.schema import LlmClassification
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
async def test_chain_tries_every_model_until_one_answers() -> None:
    """Резервов несколько: перебор не останавливается на втором (LLM-002)."""

    calls: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model != "test/fb3:free":
            return httpx.Response(503, json={"error": {"message": "down"}})
        return httpx.Response(200, json=_ok_body(content='{"label":"third"}', model=model))

    client = OpenRouterClient(
        settings=_settings(openrouter_model_fallback="test/fb1:free, test/fb2:free, test/fb3:free"),
        transport=httpx.MockTransport(route),
    )
    result = await client.complete_json(MESSAGES, schema=_Tiny)
    assert cast(_Tiny, result.data).label == "third"
    assert result.fallback_model_used is True
    assert calls == ["test/primary:free", "test/fb1:free", "test/fb2:free", "test/fb3:free"]


@pytest.mark.asyncio
async def test_chain_skips_duplicates_of_primary() -> None:
    """Та же модель в резерве — лишний вызов и лишние секунды ожидания."""

    calls: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(503, json={"error": {"message": "down"}})

    client = OpenRouterClient(
        settings=_settings(openrouter_model_fallback="test/primary:free,test/fb1:free"),
        transport=httpx.MockTransport(route),
    )
    with pytest.raises(LlmUnavailable):
        await client.complete_json(MESSAGES, schema=_Tiny)
    assert calls == ["test/primary:free", "test/fb1:free"]


@pytest.mark.asyncio
async def test_budget_stops_chain_before_inbound_window() -> None:
    """Пять моделей по 45 с пережили бы окно видимости inbound (120 с).

    Бюджет 100 с обрывает перебор: сообщение не должно вернуться в очередь
    и обработаться второй раз, пока мы ещё опрашиваем резервы.
    """

    clock = {"t": 0.0}
    calls: list[str] = []
    budgets: list[float] = []

    def now() -> float:
        return clock["t"]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        budgets.append(float(request.extensions["timeout"]["read"]))
        clock["t"] += 45.0  # каждая модель молчит до конца таймаута
        raise httpx.ReadTimeout("slow")

    client = OpenRouterClient(
        settings=_settings(
            openrouter_model_fallback="test/fb1:free,test/fb2:free,test/fb3:free,test/fb4:free",
            llm_timeout_seconds=45.0,
            llm_total_budget_seconds=100.0,
        ),
        transport=httpx.MockTransport(handler),
        time_fn=now,
    )
    with pytest.raises(LlmUnavailable):
        await client.complete_json(MESSAGES, schema=_Tiny)
    assert calls == ["test/primary:free", "test/fb1:free", "test/fb2:free"]
    # Основной — полные 45 с, резерву — не больше 20 с, последней попытке —
    # только остаток бюджета.
    assert budgets == [45.0, 20.0, 10.0]


@pytest.mark.asyncio
async def test_schema_sent_in_strict_form() -> None:
    """Все поля обязательны и лишние запрещены (CLASSIFY-003).

    Без этого pydantic оставлял `cited_fragments` вне `required`, и модель
    законно не присылала основание решения — а без основания обращение не
    маршрутизируется автоматически.
    """

    sent: dict[str, object] = {}

    answer = json.dumps(
        {
            "problem_type": "elevator",
            "responsibility_zone": "uk",
            "confidence": 0.9,
            "cited_fragments": [1],
            "reasoning": "",
            "clarifying_question": None,
            "clarifying_options": [],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content.decode()))
        return httpx.Response(200, json=_ok_body(content=answer))

    client = OpenRouterClient(settings=_settings(), transport=httpx.MockTransport(handler))
    await client.complete_json(MESSAGES, schema=LlmClassification, timeout_s=8.0)

    schema = cast(dict[str, object], sent["response_format"])
    json_schema = cast(dict[str, object], schema["json_schema"])
    body = cast(dict[str, object], json_schema["schema"])
    assert json_schema["strict"] is True
    assert body["additionalProperties"] is False
    required = cast(list[str], body["required"])
    assert set(required) == set(cast(dict[str, object], body["properties"]))
    assert "cited_fragments" in required
    defs = cast(dict[str, dict[str, object]], body["$defs"])
    assert defs, "вложенные определения должны сохраниться"


@pytest.mark.asyncio
async def test_llm_proxy_applied_only_to_openrouter_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_PROXY уходит в httpx-клиент OpenRouter — и больше никуда.

    Глобальный HTTPS_PROXY отправил бы через прокси и MAX API, и внутренний
    http://qdrant:6333; поэтому прокси — параметр именно этого клиента.
    """

    seen: dict[str, object] = {}
    real_client = httpx.AsyncClient

    def spy(*args: object, **kwargs: object) -> httpx.AsyncClient:
        seen.update(kwargs)
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, json=_ok_body(content='{"label":"ok"}'))
        )
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    client = OpenRouterClient(settings=_settings(llm_proxy="http://u:secret@proxy.test:3128"))
    await client.complete_json(MESSAGES, schema=_Tiny)
    assert seen["proxy"] == "http://u:secret@proxy.test:3128"
    assert "secret" not in repr(client)


@pytest.mark.asyncio
async def test_no_proxy_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    real_client = httpx.AsyncClient

    def spy(*args: object, **kwargs: object) -> httpx.AsyncClient:
        seen.update(kwargs)
        kwargs["transport"] = httpx.MockTransport(
            lambda _r: httpx.Response(200, json=_ok_body(content='{"label":"ok"}'))
        )
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    client = OpenRouterClient(settings=_settings())
    await client.complete_json(MESSAGES, schema=_Tiny)
    assert seen["proxy"] is None


@pytest.mark.asyncio
async def test_hanging_model_is_cut_by_wall_clock() -> None:
    """Таймаут — общий срок вызова, а не пауза между порциями байт.

    OpenRouter, пока ждёт провайдера, присылает в тело keepalive-комментарии,
    и read-таймаут httpx отсчитывается заново на каждую порцию. На замере
    LLM-002 вызов к зависшей бесплатной модели прожил 104 с при лимите 40 с —
    столько в цепочке из пяти моделей стоить нельзя.
    """

    async def slow(request: httpx.Request) -> httpx.Response:
        _ = request
        await asyncio.sleep(5.0)
        return httpx.Response(200, json=_ok_body(content='{"label":"late"}'))

    client = OpenRouterClient(
        settings=_settings(openrouter_model_fallback=None),
        transport=httpx.MockTransport(slow),
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(LlmUnavailable, match="timeout"):
        await client.complete_json(MESSAGES, schema=_Tiny, timeout_s=0.2)
    assert asyncio.get_running_loop().time() - started < 2.0


@pytest.mark.asyncio
async def test_fallback_gets_short_timeout_and_budget_can_be_narrowed() -> None:
    """Мёртвый резерв не должен висеть 45 с (прод, 29.09.2026: вопрос ждал 100 с).

    Второй вызов (консультант) получает только остаток окна — бюджет задаёт
    вызывающий.
    """

    clock = {"t": 0.0}
    calls: list[tuple[str, float]] = []

    def now() -> float:
        return clock["t"]

    def handler(request: httpx.Request) -> httpx.Response:
        read = float(request.extensions["timeout"]["read"])
        calls.append((json.loads(request.content)["model"], read))
        clock["t"] += read
        raise httpx.ReadTimeout("slow")

    client = OpenRouterClient(
        settings=_settings(
            openrouter_model_fallback="test/fb1:free,test/fb2:free,test/fb3:free",
            llm_timeout_seconds=45.0,
            llm_fallback_timeout_seconds=20.0,
            llm_total_budget_seconds=100.0,
        ),
        transport=httpx.MockTransport(handler),
        time_fn=now,
    )
    with pytest.raises(LlmUnavailable):
        await client.complete_json(MESSAGES, schema=_Tiny, budget_s=30.0)
    # Бюджет 30 с: основной — 30, на резерв остатка < 8 с уже нет.
    assert calls == [("test/primary:free", 30.0)]

    clock["t"] = 1000.0
    calls.clear()
    with pytest.raises(LlmUnavailable):
        await client.complete_json(MESSAGES, schema=_Tiny)
    assert [t for _, t in calls] == [45.0, 20.0, 20.0, 15.0]
