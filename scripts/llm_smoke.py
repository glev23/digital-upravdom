"""Живая проверка OpenRouter (LLM-001). Только с ключом в .env.

Не вызывается из check.sh. Запуск:
  uv run python scripts/llm_smoke.py
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import BaseModel, Field

from upravdom.classifier.llm import ChatMessage, LlmError, OpenRouterClient
from upravdom.config import get_settings


class SmokeSchema(BaseModel):
    problem_type: str = Field(description="код типа проблемы латиницей")
    confidence: float = Field(ge=0, le=1)


async def _run() -> int:
    settings = get_settings()
    if not settings.openrouter_api_key or not settings.openrouter_model:
        print("OPENROUTER_API_KEY / OPENROUTER_MODEL не заданы — выход", flush=True)
        return 2

    client = OpenRouterClient(settings=settings)
    messages = [
        ChatMessage(
            role="system",
            content=(
                "Верни JSON по схеме. problem_type — один из: cold_water, heating, other. "
                "confidence от 0 до 1."
            ),
        ),
        ChatMessage(
            role="user",
            content="В квартире второй день нет холодной воды из крана.",
        ),
    ]
    try:
        result = await client.complete_json(messages, schema=SmokeSchema)
    except LlmError as exc:
        print(f"FAIL {type(exc).__name__}: {exc}", flush=True)
        return 1

    print(f"model={result.model_name}", flush=True)
    print(f"fallback_used={result.fallback_model_used}", flush=True)
    print(f"latency_ms={result.latency_ms}", flush=True)
    print(f"data={result.data.model_dump()}", flush=True)
    if result.usage:
        print(f"usage={result.usage}", flush=True)
    print("OK schema valid", flush=True)
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
