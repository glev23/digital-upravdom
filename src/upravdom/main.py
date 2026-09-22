"""Точка входа FastAPI-приложения.

`/health`/`/ready` (INIT-001) + вебхук MAX и воркер inbox/outbox в том же
процессе (BOT-001, architecture.md §1 — монолит). Онбординг, классификация
и заявки подключаются далее (architecture.md §8).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from pydantic import BaseModel

from upravdom.bot_gateway.webhook import router as webhook_router
from upravdom.config import get_settings
from upravdom.db import check_database_ready
from upravdom.embeddings import warmup as warmup_embeddings
from upravdom.onboarding.texts import ensure_consent_text
from upravdom.scheduler import create_scheduler
from upravdom.vector_store import check_qdrant_ready

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    postgres: bool
    qdrant: bool


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    ensure_consent_text(settings.consent_version)
    if not settings.privacy_policy_url:
        logger.warning(
            "PRIVACY_POLICY_URL не задан — шаг согласия показывается без ссылки на политику"
        )
    try:
        await asyncio.to_thread(warmup_embeddings)
    except Exception:  # noqa: BLE001 — старт без прогрева допустим, первый запрос будет дольше
        logger.warning("прогрев эмбеддингов не удался", exc_info=True)
    scheduler = create_scheduler()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="Цифровой Управдом", lifespan=lifespan)
    app.include_router(webhook_router)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Процесс жив. Без обращения к хранилищам — это не проверка готовности."""

        return HealthResponse(status="ok")

    @app.get("/ready", response_model=ReadyResponse)
    async def ready(response: Response) -> ReadyResponse:
        """Готовность зависит от PostgreSQL и Qdrant (architecture.md §10)."""

        postgres_ok = await check_database_ready()
        qdrant_ok = await check_qdrant_ready()
        all_ok = postgres_ok and qdrant_ok
        response.status_code = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(
            status="ok" if all_ok else "unavailable",
            postgres=postgres_ok,
            qdrant=qdrant_ok,
        )

    return app


app = create_app()
