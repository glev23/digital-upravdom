"""Идемпотентность load_kb и синхронизация с манифестом."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.knowledge.artifacts import KB_COLLECTION, load_manifest
from upravdom.knowledge.loader import load_kb_from_artifacts
from upravdom.models import KnowledgeChunk, KnowledgeDocument
from upravdom.models.enums import IndexState
from upravdom.vector_store import get_qdrant_client

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def qdrant() -> AsyncIterator[AsyncQdrantClient]:
    client = get_qdrant_client()
    names = {c.name for c in (await client.get_collections()).collections}
    if KB_COLLECTION in names:
        await client.delete_collection(KB_COLLECTION)
    yield client


async def test_load_kb_idempotent(session: AsyncSession, qdrant: AsyncQdrantClient) -> None:
    stats1 = await load_kb_from_artifacts(session, qdrant)
    n_docs = await session.scalar(select(func.count()).select_from(KnowledgeDocument))
    n_chunks = await session.scalar(select(func.count()).select_from(KnowledgeChunk))
    assert stats1["count"] == load_manifest()["count"]
    assert n_chunks == stats1["count"]
    assert n_docs == stats1["docs"]

    stats2 = await load_kb_from_artifacts(session, qdrant)
    n_chunks2 = await session.scalar(select(func.count()).select_from(KnowledgeChunk))
    assert n_chunks2 == n_chunks
    assert stats2["obsolete_removed"] == 0

    info = await qdrant.get_collection(KB_COLLECTION)
    assert info.points_count == load_manifest()["count"]

    indexed = await session.scalar(
        select(func.count())
        .select_from(KnowledgeChunk)
        .where(KnowledgeChunk.index_state == IndexState.INDEXED)
    )
    assert indexed == n_chunks


async def test_obsolete_chunks_removed_from_both_stores(
    session: AsyncSession, qdrant: AsyncQdrantClient
) -> None:
    await load_kb_from_artifacts(session, qdrant)
    fake_id = uuid.uuid4()
    doc_id = (await session.scalars(select(KnowledgeDocument.id).limit(1))).one()
    await session.execute(
        text(
            "INSERT INTO knowledge_chunks "
            "(id, document_id, chunk_text, chunk_meta, qdrant_point_id, index_state, created_at) "
            "VALUES (:id, :doc, 'obsolete', '{}', :id, 'indexed', now())"
        ),
        {"id": fake_id, "doc": doc_id},
    )
    await qdrant.upsert(
        collection_name=KB_COLLECTION,
        points=[
            qm.PointStruct(
                id=str(fake_id),
                vector=[0.0] * 768,
                payload={"chunk_id": str(fake_id), "ref": "fake"},
            )
        ],
    )
    await session.commit()

    stats = await load_kb_from_artifacts(session, qdrant)
    assert stats["obsolete_removed"] >= 1
    left = await session.scalar(select(KnowledgeChunk.id).where(KnowledgeChunk.id == fake_id))
    assert left is None
    points = await qdrant.retrieve(collection_name=KB_COLLECTION, ids=[str(fake_id)])
    assert points == []
