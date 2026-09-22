"""Полная пересборка коллекции kb_chunks из Postgres (architecture.md §9)."""

from __future__ import annotations

import asyncio
import logging

from qdrant_client.http import models as qm
from sqlalchemy import select

from upravdom.embeddings import PROMPT_SEARCH_DOCUMENT, embed
from upravdom.knowledge.artifacts import KB_COLLECTION, VECTOR_DIM
from upravdom.knowledge.loader import ensure_kb_collection
from upravdom.models import KnowledgeChunk, KnowledgeDocument
from upravdom.models.enums import IndexState

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reindex")


async def _run() -> int:
    from upravdom.db import session_scope
    from upravdom.vector_store import get_qdrant_client

    client = get_qdrant_client()
    # Удалить и создать коллекцию заново
    names = {c.name for c in (await client.get_collections()).collections}
    if KB_COLLECTION in names:
        await client.delete_collection(KB_COLLECTION)
    await ensure_kb_collection(client)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(KnowledgeChunk, KnowledgeDocument).join(
                    KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id
                )
            )
        ).all()
        if not rows:
            logger.warning("no chunks in Postgres — nothing to reindex")
            return 0

        texts = [chunk.chunk_text for chunk, _doc in rows]
        print(f"[reindex] embedding {len(texts)} chunks…", flush=True)
        import numpy as np

        vectors = np.zeros((len(texts), VECTOR_DIM), dtype=np.float32)
        batch = 16
        for i in range(0, len(texts), batch):
            part = texts[i : i + batch]
            vectors[i : i + len(part)] = embed(part, prompt=PROMPT_SEARCH_DOCUMENT)

        points: list[qm.PointStruct] = []
        for i, (chunk, doc) in enumerate(rows):
            payload = {
                "chunk_id": str(chunk.id),
                "document_id": str(doc.id),
                "source_key": doc.source_key,
                "ref": (chunk.chunk_meta or {}).get("ref"),
                "chunk_text": chunk.chunk_text,
                "effective_from": doc.effective_from.isoformat() if doc.effective_from else None,
                "effective_to": doc.effective_to.isoformat() if doc.effective_to else None,
                "region_code": doc.region_code,
                "management_company_id": (
                    str(doc.management_company_id) if doc.management_company_id else None
                ),
            }
            points.append(
                qm.PointStruct(id=str(chunk.id), vector=vectors[i].tolist(), payload=payload)
            )
            chunk.qdrant_point_id = chunk.id
            chunk.index_state = IndexState.INDEXED

        for i in range(0, len(points), 64):
            await client.upsert(collection_name=KB_COLLECTION, points=points[i : i + 64])
        await session.commit()

    info = await client.get_collection(KB_COLLECTION)
    print(f"[reindex] OK points={info.points_count}", flush=True)
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception:  # noqa: BLE001
        logging.exception("reindex failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
