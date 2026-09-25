"""HTTP-клиент OpenRouter (LLM-001)."""

from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from upravdom.classifier.llm.port import (
    ChatMessage,
    LlmBadResponse,
    LlmError,
    LlmNotConfigured,
    LlmRateLimited,
    LlmResult,
    LlmUnavailable,
    LlmUsage,
    messages_as_dicts,
)
from upravdom.config import Settings, get_settings

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_CONNECT_TIMEOUT_S = 3.0
_FENCE_RE = re.compile(r"^```(?:json)?\s*([\s\S]*?)\s*```$", re.IGNORECASE)


class _CircuitBreaker:
    def __init__(self, *, failure_threshold: int, open_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._failures = 0
        self._opened_at: float | None = None

    def allow(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self._opened_at is None:
            return True
        if now - self._opened_at >= self._open_seconds:
            return True  # half-open: разрешаем пробный вызов
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._opened_at = now


class _TokenBucket:
    """Глобальный лимит исходящих LLM-вызовов (без ожидания)."""

    def __init__(self, *, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._timestamps: list[float] = []

    def allow(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - 60.0
        self._timestamps = [t for t in self._timestamps if t >= cutoff]
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True


class OpenRouterClient:
    """Асинхронный клиент; ключ не попадает в repr/логи."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = _OPENROUTER_URL,
        time_fn: Any = time.monotonic,
    ) -> None:
        self._settings = settings or get_settings()
        self._base_url = base_url
        self._transport = transport
        self._time = time_fn
        # Размыкатель на каждую модель, а не общий: иначе успех резервной модели
        # обнулял бы счётчик сбоев основной, и при пропавшей бесплатной основной
        # модели каждое сообщение ждало бы её полный таймаут перед резервом.
        self._breakers: dict[str, _CircuitBreaker] = {}
        self._bucket = _TokenBucket(max_per_minute=self._settings.llm_max_requests_per_minute)

    def _breaker(self, model: str) -> _CircuitBreaker:
        breaker = self._breakers.get(model)
        if breaker is None:
            breaker = _CircuitBreaker(
                failure_threshold=self._settings.llm_circuit_failure_threshold,
                open_seconds=self._settings.llm_circuit_open_seconds,
            )
            self._breakers[model] = breaker
        return breaker

    def __repr__(self) -> str:
        return (
            f"OpenRouterClient(model={self._settings.openrouter_model!r}, "
            f"fallback={self._settings.openrouter_model_fallback!r})"
        )

    def _ensure_configured(self) -> tuple[str, str]:
        key = self._settings.openrouter_api_key
        model = self._settings.openrouter_model
        if not key or not model:
            raise LlmNotConfigured("OPENROUTER_API_KEY/OPENROUTER_MODEL не заданы")
        return key, model

    def _headers(self, api_key: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if self._settings.public_base_url:
            headers["HTTP-Referer"] = self._settings.public_base_url
        headers["X-Title"] = "Digital Upravdom"
        return headers

    async def complete_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: type[BaseModel],
        timeout_s: float | None = None,
    ) -> LlmResult:
        api_key, primary = self._ensure_configured()
        timeout_s = self._settings.llm_timeout_seconds if timeout_s is None else timeout_s
        fallback = self._settings.openrouter_model_fallback
        now = self._time()

        # Порядок попыток: основная, затем резервная — каждая только если её
        # размыкатель закрыт. Открытый размыкатель основной = сразу резерв.
        candidates = [(primary, False)]
        if fallback and fallback != primary:
            candidates.append((fallback, True))
        allowed = [(m, is_fb) for m, is_fb in candidates if self._breaker(m).allow(now=now)]
        if not allowed:
            raise LlmUnavailable("circuit breaker open")
        # Один слот лимита на вызов complete_json, резервная попытка его не тратит.
        if not self._bucket.allow(now=now):
            raise LlmRateLimited("local LLM rate limit exceeded")

        t0 = self._time()
        last_exc: LlmError | None = None
        for model, is_fallback in allowed:
            if last_exc is not None:
                logger.info(
                    "LLM primary failed (%s), trying fallback model", type(last_exc).__name__
                )
            try:
                data, model_name, usage = await self._call(
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    schema=schema,
                    timeout_s=timeout_s,
                )
            except (LlmUnavailable, LlmRateLimited, LlmBadResponse) as exc:
                self._breaker(model).record_failure(now=self._time())
                last_exc = exc
                continue
            self._breaker(model).record_success()
            return LlmResult(
                data=data,
                model_name=model_name,
                fallback_model_used=is_fallback,
                latency_ms=int((self._time() - t0) * 1000),
                usage=usage,
            )
        assert last_exc is not None
        raise last_exc

    async def _call(
        self,
        *,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        schema: type[BaseModel],
        timeout_s: float,
    ) -> tuple[BaseModel, str, LlmUsage | None]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages_as_dicts(messages),
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": _as_strict_schema(schema.model_json_schema()),
                },
            },
        }
        timeout = httpx.Timeout(timeout_s, connect=_CONNECT_TIMEOUT_S)
        # С явным transport (тесты) прокси не подключается: в httpx прокси —
        # это mount, он перехватил бы запрос раньше подставленного transport.
        proxy = self._settings.llm_proxy if self._transport is None else None
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=timeout, proxy=proxy or None
            ) as client:
                response = await client.post(
                    self._base_url, headers=self._headers(api_key), json=body
                )
        except httpx.TimeoutException as exc:
            raise LlmUnavailable("timeout") from exc
        except httpx.TransportError as exc:
            raise LlmUnavailable("network error") from exc

        if response.status_code == 429:
            raise LlmRateLimited("provider 429")
        if response.status_code >= 500:
            raise LlmUnavailable(f"provider {response.status_code}")
        if response.status_code >= 400:
            # 401/402/403 — недоступность для деградации
            raise LlmUnavailable(f"provider {response.status_code}")

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise LlmBadResponse("response is not JSON") from exc

        if not isinstance(payload, dict):
            raise LlmBadResponse("response is not a JSON object")
        if isinstance(payload.get("error"), dict):
            err = payload["error"]
            code = err.get("code")
            if code == 429:
                raise LlmRateLimited(str(err.get("message") or "429"))
            raise LlmUnavailable(str(err.get("message") or "error in body"))

        choices = payload.get("choices") or []
        if not choices:
            raise LlmBadResponse("empty choices")
        choice = choices[0]
        if isinstance(choice.get("error"), dict):
            raise LlmUnavailable(str(choice["error"].get("message") or "choice error"))

        message = choice.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LlmBadResponse("empty content")

        parsed = _parse_json_content(content)
        try:
            data = schema.model_validate(parsed)
        except ValidationError as exc:
            raise LlmBadResponse("schema validation failed") from exc

        model_name = str(payload.get("model") or model)
        usage = _parse_usage(payload.get("usage"))
        logger.info(
            "LLM ok model=%s latency_budget_s=%s",
            model_name,
            timeout_s,
        )
        return data, model_name, usage


def _as_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Привести JSON-схему pydantic к строгому режиму провайдеров.

    Pydantic кладёт в ``required`` только поля без значения по умолчанию,
    поэтому ``cited_fragments`` — основание решения — формально оставался
    необязательным, и модель законно его пропускала (CLASSIFY-003: на живом
    прогоне поле чаще всего пустое, а без него правило «нет подтверждённой
    нормы — нет авто-маршрутизации» отправляет обращение в «не уверен»).

    Пустой список по-прежнему допустим: обязанность **назвать** основание не
    должна превращаться в обязанность его выдумать, когда поиск не дал
    ничего релевантного (тот же риск, что в §6.4 у справки о правах).
    """

    out = dict(schema)
    for key in ("$defs", "definitions"):
        defs = out.get(key)
        if isinstance(defs, dict):
            out[key] = {name: _as_strict_schema(sub) for name, sub in defs.items()}
    properties = out.get("properties")
    if isinstance(properties, dict):
        out["properties"] = {name: _as_strict_schema(sub) for name, sub in properties.items()}
        out["required"] = list(properties)
        out["additionalProperties"] = False
    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = _as_strict_schema(items)
    return out


def _parse_json_content(content: str) -> Any:
    text = content.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmBadResponse("content is not JSON") from exc


def _parse_usage(raw: Any) -> LlmUsage | None:
    if not isinstance(raw, dict):
        return None
    return LlmUsage(
        prompt_tokens=_as_int(raw.get("prompt_tokens")),
        completion_tokens=_as_int(raw.get("completion_tokens")),
        total_tokens=_as_int(raw.get("total_tokens")),
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None


@lru_cache
def get_llm_client() -> OpenRouterClient:
    """Один клиент на процесс.

    Лимит частоты и размыкатель живут в экземпляре: клиент, созданный заново
    на каждое сообщение, начинал бы счёт «15 в минуту» и «3 сбоя» с нуля —
    защита LLM-001 в рабочем режиме не срабатывала бы вовсе.
    """

    return OpenRouterClient()
