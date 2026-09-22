"""Поиск по базе знаний с фильтром действующей редакции (KB-001)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date
from functools import partial

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from upravdom.embeddings import PROMPT_SEARCH_QUERY, embed
from upravdom.knowledge.artifacts import KB_COLLECTION
from upravdom.vector_store import get_qdrant_client


@dataclass(slots=True, frozen=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    ref: str
    text: str
    score: float
    source_key: str | None = None

    @property
    def label(self) -> str:
        """Полная ссылка с документом («ПП РФ №491, п. 5 абз. 1»).

        Чанкер кладёт её первой строкой текста; `ref` — только пункт без
        документа, жителю его показывать нельзя.
        """

        first, _, _ = self.text.partition("\n")
        return first.strip() or self.ref

    @property
    def body(self) -> str:
        """Текст пункта без строки-заголовка."""

        first, sep, rest = self.text.partition("\n")
        return rest.strip() if sep else first.strip()


def _query_filter(on_date: date, management_company_id: uuid.UUID | None) -> qm.Filter:
    company_should: list[qm.Condition] = [
        qm.IsNullCondition(is_null=qm.PayloadField(key="management_company_id")),
    ]
    if management_company_id is not None:
        company_should.append(
            qm.FieldCondition(
                key="management_company_id",
                match=qm.MatchValue(value=str(management_company_id)),
            )
        )
    return qm.Filter(
        must=[
            qm.FieldCondition(key="effective_from", range=qm.DatetimeRange(lte=on_date)),
            qm.Filter(
                should=[
                    qm.IsNullCondition(is_null=qm.PayloadField(key="effective_to")),
                    qm.FieldCondition(key="effective_to", range=qm.DatetimeRange(gt=on_date)),
                ]
            ),
            qm.Filter(should=company_should),
        ]
    )


async def search(
    query: str,
    *,
    k: int = 5,
    on_date: date | None = None,
    management_company_id: uuid.UUID | None = None,
    client: AsyncQdrantClient | None = None,
) -> list[RetrievedChunk]:
    """top-k чанков; текст берётся из payload (кладётся при загрузке)."""

    on_date = on_date or date.today()
    client = client or get_qdrant_client()
    # embed() синхронный (~490 МБ модель) — не блокируем event loop (CLASSIFY-001).
    vectors = await asyncio.to_thread(partial(embed, [query], prompt=PROMPT_SEARCH_QUERY))
    vector = vectors[0].tolist()
    hits = await client.query_points(
        collection_name=KB_COLLECTION,
        query=vector,
        query_filter=_query_filter(on_date, management_company_id),
        limit=k,
        with_payload=True,
    )
    results: list[RetrievedChunk] = []
    for point in hits.points:
        payload = point.payload or {}
        results.append(
            RetrievedChunk(
                chunk_id=uuid.UUID(str(payload.get("chunk_id") or point.id)),
                ref=str(payload.get("ref") or ""),
                text=str(payload.get("chunk_text") or ""),
                score=float(point.score or 0.0),
                source_key=(
                    str(payload["source_key"]) if payload.get("source_key") is not None else None
                ),
            )
        )
    return results
