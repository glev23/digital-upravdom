"""Порт LLM-клиента (LLM-001): интерфейс, результат, ошибки."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class LlmError(Exception):
    """Базовый отказ LLM — CLASSIFY-001 ловит и деградирует."""


class LlmNotConfigured(LlmError):
    """Нет ключа или модели — классификация сразу в деградацию."""


class LlmUnavailable(LlmError):
    """Таймаут, сеть, 5xx, размыкатель открыт."""


class LlmRateLimited(LlmError):
    """429 провайдера или локальный лимит."""


class LlmBadResponse(LlmError):
    """Ответ не JSON или не проходит схему."""


@dataclass(slots=True, frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(slots=True, frozen=True)
class LlmUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(slots=True, frozen=True)
class LlmResult:
    data: BaseModel
    model_name: str
    fallback_model_used: bool
    latency_ms: int
    usage: LlmUsage | None = None


class LlmClient(Protocol):
    async def complete_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: type[BaseModel],
        timeout_s: float | None = None,
    ) -> LlmResult: ...


def messages_as_dicts(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    return [{"role": m.role, "content": m.content} for m in messages]
