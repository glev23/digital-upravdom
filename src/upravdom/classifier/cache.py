"""Семантический кэш решений классификации (CLASSIFY-002, architecture.md §6.3)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from upravdom.embeddings import PROMPT_CLASSIFICATION, embed
from upravdom.models.enums import ResponsibilityZone
from upravdom.vector_store import get_qdrant_client

logger = logging.getLogger(__name__)

CACHE_COLLECTION = "semantic_cache"
VECTOR_DIM = 768

# Поля, которые нельзя класть в payload (текст обращения, адресат).
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "text",
        "message",
        "message_masked",
        "raw_text",
        "addressee",
        "routed_to_org_id",
        "management_company_id",
        "house_id",
        "ads_phone",
        "contact",
    }
)


@dataclass(slots=True, frozen=True)
class CacheHit:
    problem_type: str
    responsibility_zone: ResponsibilityZone
    confidence: float
    chunk_ids: list[str]
    model_name: str


async def ensure_cache_collection(client: AsyncQdrantClient | None = None) -> None:
    client = client or get_qdrant_client()
    names = {c.name for c in (await client.get_collections()).collections}
    if CACHE_COLLECTION not in names:
        await client.create_collection(
            collection_name=CACHE_COLLECTION,
            vectors_config=qm.VectorParams(size=VECTOR_DIM, distance=qm.Distance.COSINE),
        )
    for field in ("model_name", "prompt_version", "kb_version"):
        try:
            await client.create_payload_index(
                collection_name=CACHE_COLLECTION,
                field_name=field,
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        except Exception:  # noqa: BLE001 — индекс уже есть
            pass


def _embed_masked(masked: str) -> list[float]:
    vector: list[float] = embed([masked], prompt=PROMPT_CLASSIFICATION)[0].tolist()
    return vector


async def lookup_cache(
    masked: str,
    *,
    model_name: str,
    prompt_version: str,
    kb_version: str,
    threshold: float,
    client: AsyncQdrantClient | None = None,
) -> CacheHit | None:
    """Ближайший сосед при cosine ≥ threshold и совпадении конфигурации."""

    import asyncio

    client = client or get_qdrant_client()
    try:
        await ensure_cache_collection(client)
        vector = await asyncio.to_thread(partial(_embed_masked, masked))
        hits = await client.query_points(
            collection_name=CACHE_COLLECTION,
            query=vector,
            query_filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="model_name", match=qm.MatchValue(value=model_name)),
                    qm.FieldCondition(
                        key="prompt_version", match=qm.MatchValue(value=prompt_version)
                    ),
                    qm.FieldCondition(key="kb_version", match=qm.MatchValue(value=kb_version)),
                ]
            ),
            limit=1,
            with_payload=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning("semantic_cache: lookup недоступен", exc_info=True)
        return None

    if not hits.points:
        return None
    point = hits.points[0]
    if float(point.score or 0.0) < threshold:
        return None
    payload = point.payload or {}
    if _FORBIDDEN_PAYLOAD_KEYS & payload.keys():
        logger.error("semantic_cache: запрещённые ключи в payload — промах")
        return None
    chunk_ids = [str(x) for x in (payload.get("chunk_ids") or [])]
    if not chunk_ids:
        return None
    try:
        zone = ResponsibilityZone(str(payload["responsibility_zone"]))
        return CacheHit(
            problem_type=str(payload["problem_type"]),
            responsibility_zone=zone,
            confidence=float(payload["confidence"]),
            chunk_ids=chunk_ids,
            model_name=str(payload.get("model_name") or model_name),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("semantic_cache: битый payload", exc_info=True)
        return None


async def store_cache(
    masked: str,
    *,
    problem_type: str,
    responsibility_zone: ResponsibilityZone,
    confidence: float,
    chunk_ids: list[str],
    model_name: str,
    prompt_version: str,
    kb_version: str,
    client: AsyncQdrantClient | None = None,
) -> None:
    """Сохранить решение auto основной модели. Без текста обращения."""

    import asyncio

    if not chunk_ids:
        return
    client = client or get_qdrant_client()
    payload: dict[str, Any] = {
        "problem_type": problem_type,
        "responsibility_zone": responsibility_zone.value,
        "confidence": confidence,
        "chunk_ids": chunk_ids,
        "model_name": model_name,
        "prompt_version": prompt_version,
        "kb_version": kb_version,
        "created_at": datetime.now(UTC).isoformat(),
    }
    assert not (_FORBIDDEN_PAYLOAD_KEYS & payload.keys())
    try:
        await ensure_cache_collection(client)
        vector = await asyncio.to_thread(partial(_embed_masked, masked))
        await client.upsert(
            collection_name=CACHE_COLLECTION,
            points=[
                qm.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vector,
                    payload=payload,
                )
            ],
        )
    except Exception:  # noqa: BLE001 — кэш best-effort
        logger.warning("semantic_cache: запись не удалась", exc_info=True)
