"""Синхронизация Postgres + Qdrant с манифестом `data/kb/` (KB-001)."""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.config import get_settings
from upravdom.knowledge.artifacts import (
    KB_COLLECTION,
    VECTOR_DIM,
    ArtifactChunk,
    load_chunks,
    load_manifest,
    load_vectors,
)
from upravdom.models import KnowledgeChunk, KnowledgeDocument
from upravdom.models.enums import IndexState
from upravdom.vector_store import get_qdrant_client

logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


async def ensure_kb_collection(client: AsyncQdrantClient) -> None:
    names = {c.name for c in (await client.get_collections()).collections}
    if KB_COLLECTION in names:
        return
    await client.create_collection(
        collection_name=KB_COLLECTION,
        vectors_config=qm.VectorParams(size=VECTOR_DIM, distance=qm.Distance.COSINE),
    )
    for field, schema in (
        ("source_key", qm.PayloadSchemaType.KEYWORD),
        ("ref", qm.PayloadSchemaType.KEYWORD),
        ("document_id", qm.PayloadSchemaType.KEYWORD),
        ("chunk_id", qm.PayloadSchemaType.KEYWORD),
        ("region_code", qm.PayloadSchemaType.KEYWORD),
        ("management_company_id", qm.PayloadSchemaType.KEYWORD),
        ("effective_from", qm.PayloadSchemaType.DATETIME),
        ("effective_to", qm.PayloadSchemaType.DATETIME),
    ):
        await client.create_payload_index(
            collection_name=KB_COLLECTION, field_name=field, field_schema=schema
        )


def _point_payload(ch: ArtifactChunk) -> dict[str, Any]:
    doc = ch.document
    return {
        "chunk_id": ch.chunk_id,
        "document_id": ch.document_id,
        "source_key": doc["source_key"],
        "ref": ch.chunk_meta.get("ref"),
        "chunk_text": ch.chunk_text,
        "effective_from": doc.get("effective_from"),
        "effective_to": doc.get("effective_to"),
        "region_code": doc.get("region_code"),
        "management_company_id": doc.get("management_company_id"),
    }


async def _upsert_chunk_row(session: AsyncSession, ch: ArtifactChunk, *, indexed: bool) -> None:
    cid = uuid.UUID(ch.chunk_id)
    state = IndexState.INDEXED if indexed else IndexState.FAILED
    point_id = cid if indexed else None
    await session.execute(
        pg_insert(KnowledgeChunk)
        .values(
            id=cid,
            document_id=uuid.UUID(ch.document_id),
            chunk_text=ch.chunk_text,
            chunk_meta=ch.chunk_meta,
            qdrant_point_id=point_id,
            index_state=state,
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_={
                "document_id": uuid.UUID(ch.document_id),
                "chunk_text": ch.chunk_text,
                "chunk_meta": ch.chunk_meta,
                "qdrant_point_id": point_id,
                "index_state": state,
            },
        )
    )


async def load_kb_from_artifacts(
    session: AsyncSession, client: AsyncQdrantClient
) -> dict[str, int]:
    """Синхронизирует состояние ровно с манифестом. Возвращает счётчики."""

    manifest = load_manifest()
    settings = get_settings()
    if manifest.get("dim") != VECTOR_DIM:
        msg = f"manifest dim={manifest.get('dim')} != runtime {VECTOR_DIM}"
        raise RuntimeError(msg)
    if manifest.get("model_name") != settings.embedding_model_name:
        msg = (
            f"manifest model_name={manifest.get('model_name')!r} != "
            f"runtime {settings.embedding_model_name!r}"
        )
        raise RuntimeError(msg)

    chunks = load_chunks()
    vectors = load_vectors()
    if len(chunks) != vectors.shape[0] or len(chunks) != int(manifest["count"]):
        msg = (
            f"рассинхрон артефактов: chunks={len(chunks)}, "
            f"vectors={vectors.shape[0]}, manifest.count={manifest['count']}"
        )
        raise RuntimeError(msg)

    await ensure_kb_collection(client)

    wanted_chunk_ids = {uuid.UUID(ch.chunk_id) for ch in chunks}
    wanted_doc_ids = {uuid.UUID(ch.document_id) for ch in chunks}

    docs_by_id: dict[uuid.UUID, dict[str, Any]] = {}
    for ch in chunks:
        docs_by_id[uuid.UUID(ch.document_id)] = ch.document

    for doc_id, doc in docs_by_id.items():
        mc_raw = doc.get("management_company_id")
        await session.execute(
            pg_insert(KnowledgeDocument)
            .values(
                id=doc_id,
                source_key=doc["source_key"],
                title=doc["title"],
                version=doc["version"],
                effective_from=_parse_date(doc["effective_from"]),
                effective_to=_parse_date(doc.get("effective_to")),
                region_code=doc.get("region_code"),
                management_company_id=uuid.UUID(mc_raw) if mc_raw else None,
                checksum=manifest["kb_version"],
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )

    rows = (await session.execute(select(KnowledgeChunk.id, KnowledgeChunk.qdrant_point_id))).all()
    existing_ids = {row[0] for row in rows}
    obsolete = existing_ids - wanted_chunk_ids
    if obsolete:
        await client.delete(
            collection_name=KB_COLLECTION,
            points_selector=qm.PointIdsList(points=[str(i) for i in obsolete]),
        )
        await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.id.in_(obsolete)))

    orphan_docs = (
        await session.scalars(
            select(KnowledgeDocument.id).where(~KnowledgeDocument.id.in_(wanted_doc_ids))
        )
    ).all()
    for od in orphan_docs:
        still = await session.scalar(
            select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == od).limit(1)
        )
        if still is None:
            await session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == od))

    upserted = 0
    failed = 0
    batch_points: list[qm.PointStruct] = []
    batch_chunks: list[ArtifactChunk] = []

    async def flush() -> None:
        nonlocal upserted, failed, batch_points, batch_chunks
        if not batch_points:
            return
        try:
            await client.upsert(collection_name=KB_COLLECTION, points=batch_points)
            for ch in batch_chunks:
                await _upsert_chunk_row(session, ch, indexed=True)
            upserted += len(batch_chunks)
        except Exception:  # noqa: BLE001
            logger.exception("qdrant upsert failed for %s points", len(batch_chunks))
            for ch in batch_chunks:
                await _upsert_chunk_row(session, ch, indexed=False)
            failed += len(batch_chunks)
        batch_points = []
        batch_chunks = []

    for i, ch in enumerate(chunks):
        cid = uuid.UUID(ch.chunk_id)
        batch_points.append(
            qm.PointStruct(id=str(cid), vector=vectors[i].tolist(), payload=_point_payload(ch))
        )
        batch_chunks.append(ch)
        if len(batch_points) >= 64:
            await flush()
    await flush()
    await session.commit()

    return {
        "count": len(chunks),
        "upserted": upserted,
        "obsolete_removed": len(obsolete),
        "failed": failed,
        "docs": len(docs_by_id),
    }


async def load_kb_safe() -> dict[str, int] | None:
    """Загрузка для entrypoint: ошибка логируется, процесс не падает."""

    from upravdom.db import session_scope

    try:
        client = get_qdrant_client()
        async with session_scope() as session:
            stats = await load_kb_from_artifacts(session, client)
        logger.info("KB loaded: %s", stats)
        return stats
    except Exception:  # noqa: BLE001
        logger.exception("KB load failed — app continues without fresh index")
        return None
