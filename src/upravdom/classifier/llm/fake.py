"""FakeLlmClient для тестов CLASSIFY-001 / LLM-001."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from upravdom.classifier.llm.port import (
    ChatMessage,
    LlmBadResponse,
    LlmRateLimited,
    LlmResult,
    LlmUnavailable,
    LlmUsage,
)


class FakeLlmClient:
    """Сценарии задаются очередью ответов/исключений."""

    def __init__(
        self,
        *,
        responses: list[BaseModel | Exception | Callable[[], BaseModel | Exception]] | None = None,
        model_name: str = "fake/model",
    ) -> None:
        self._queue = list(responses or [])
        self.calls: list[list[ChatMessage]] = []
        self.model_name = model_name

    def enqueue(self, item: BaseModel | Exception | Callable[[], BaseModel | Exception]) -> None:
        self._queue.append(item)

    async def complete_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: type[BaseModel],
        timeout_s: float | None = None,
    ) -> LlmResult:
        _ = schema, timeout_s
        self.calls.append(messages)
        if not self._queue:
            raise LlmUnavailable("fake queue empty")
        item = self._queue.pop(0)
        if callable(item) and not isinstance(item, BaseModel):
            item = item()
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, BaseModel)
        return LlmResult(
            data=item,
            model_name=self.model_name,
            fallback_model_used=False,
            latency_ms=1,
            usage=LlmUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


# Удобные фабрики ошибок для сценариев CLASSIFY-001
def timeout() -> LlmUnavailable:
    return LlmUnavailable("timeout")


def rate_limited() -> LlmRateLimited:
    return LlmRateLimited("429")


def garbage() -> LlmBadResponse:
    return LlmBadResponse("not json")
