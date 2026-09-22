"""Семантический кэш CLASSIFY-002: попадание, инвалидация, что не пишется."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from qdrant_client import AsyncQdrantClient
from scripts.seed_demo import seed, stable_id
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.classifier.cache import (
    CACHE_COLLECTION,
    ensure_cache_collection,
    lookup_cache,
    store_cache,
)
from upravdom.classifier.llm.fake import FakeLlmClient
from upravdom.classifier.prompt import PROMPT_VERSION
from upravdom.classifier.schema import Branch, Clarification, LlmClassification
from upravdom.classifier.service import classify
from upravdom.config import get_settings
from upravdom.knowledge.artifacts import load_manifest
from upravdom.knowledge.loader import load_kb_from_artifacts
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.masking import mask
from upravdom.models import ClassificationLog, KnowledgeChunk
from upravdom.models.enums import ResponsibilityZone
from upravdom.vector_store import get_qdrant_client

pytestmark = pytest.mark.asyncio

_PROBLEM_TYPES = Path(__file__).resolve().parents[2] / "data" / "seed" / "problem_types.json"

_UPSERT_PT = text(
    """
    INSERT INTO problem_types
        (code, title, default_responsibility_zone, resolution_hours, norm_reference)
    VALUES
        (:code, :title, :default_responsibility_zone, :resolution_hours, :norm_reference)
    ON CONFLICT (code) DO UPDATE SET
        title = EXCLUDED.title,
        default_responsibility_zone = EXCLUDED.default_responsibility_zone,
        resolution_hours = EXCLUDED.resolution_hours,
        norm_reference = EXCLUDED.norm_reference
    """
)


async def _seed_pt(session: AsyncSession) -> None:
    import json

    for row in json.loads(_PROBLEM_TYPES.read_text(encoding="utf-8")):
        await session.execute(_UPSERT_PT, row)


async def _inbound(session: AsyncSession) -> uuid.UUID:
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO inbound_events "
            "(id, max_event_id, payload, status, attempts, received_at) "
            "VALUES (:id, :m, '{}', 'pending', 0, :ts)"
        ),
        {"id": event_id, "m": f"cache-{event_id}", "ts": datetime.now(UTC)},
    )
    return event_id


@pytest.fixture
async def cache_env(
    session: AsyncSession,
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, AsyncQdrantClient, str, RetrievedChunk]]:
    await _seed_pt(session)
    await seed(session)
    client = get_qdrant_client()
    names = {c.name for c in (await client.get_collections()).collections}
    if CACHE_COLLECTION in names:
        await client.delete_collection(CACHE_COLLECTION)
    await ensure_cache_collection(client)
    await load_kb_from_artifacts(session, client)
    chunk = (await session.execute(select(KnowledgeChunk).limit(1))).scalar_one()
    meta = chunk.chunk_meta or {}
    retrieved = RetrievedChunk(
        chunk_id=chunk.id,
        ref=str(meta.get("ref") or "п. 5"),
        text=chunk.chunk_text,
        score=0.99,
    )
    house_id = stable_id("house:house-dekabristov-10")
    event_id = await _inbound(session)
    await session.commit()
    kb_ver = str(load_manifest()["kb_version"])
    yield house_id, event_id, client, kb_ver, retrieved


def _llm_auto(ref: str) -> LlmClassification:
    return LlmClassification(
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.95,
        cited_fragments=[1],
        reasoning="тест кэша",
        clarifying_question=None,
        clarifying_options=[],
    )


async def _search_fixed(
    retrieved: RetrievedChunk,
) -> object:
    async def _fn(*_a: object, **_k: object) -> list[RetrievedChunk]:
        return [retrieved]

    return _fn


async def test_cache_hit_skips_llm(
    session: AsyncSession,
    cache_env: tuple[uuid.UUID, uuid.UUID, AsyncQdrantClient, str, RetrievedChunk],
) -> None:
    house_id, event_id, client, kb_ver, retrieved = cache_env
    settings = get_settings()
    assert settings.openrouter_model
    search_fn = await _search_fixed(retrieved)
    llm = FakeLlmClient(responses=[_llm_auto(retrieved.ref)])

    first = await classify(
        "нет холодной воды из крана, зовите Иванова +79031112233",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=search_fn,  # type: ignore[arg-type]
        settings=settings,
    )
    assert first.branch is Branch.AUTO
    assert len(llm.calls) == 1

    event2 = await _inbound(session)
    llm2 = FakeLlmClient(responses=[_llm_auto(retrieved.ref)])
    second = await classify(
        "нет холодной воды из крана, зовите Петрова +79039998877",
        house_id=house_id,
        inbound_event_id=event2,
        session=session,
        llm=llm2,
        search_fn=search_fn,  # type: ignore[arg-type]
        settings=settings,
    )
    assert second.branch is Branch.AUTO
    assert llm2.calls == []
    row = (
        await session.execute(
            select(ClassificationLog).where(ClassificationLog.id == second.log_id)
        )
    ).scalar_one()
    assert row.cache_hit is True
    _ = client, kb_ver


async def test_invalidate_by_model(
    session: AsyncSession,
    cache_env: tuple[uuid.UUID, uuid.UUID, AsyncQdrantClient, str, RetrievedChunk],
) -> None:
    _house, _event, client, kb_ver, retrieved = cache_env
    settings = get_settings()
    masked = str(mask("нет холодной воды из крана совсем").text)
    await store_cache(
        masked,
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.95,
        chunk_ids=[str(retrieved.chunk_id)],
        model_name=settings.openrouter_model or "m",
        prompt_version=PROMPT_VERSION,
        kb_version=kb_ver,
        client=client,
    )
    miss = await lookup_cache(
        masked,
        model_name="other/model:free",
        prompt_version=PROMPT_VERSION,
        kb_version=kb_ver,
        threshold=settings.semantic_cache_threshold,
        client=client,
    )
    assert miss is None


async def test_invalidate_by_prompt_version(
    session: AsyncSession,
    cache_env: tuple[uuid.UUID, uuid.UUID, AsyncQdrantClient, str, RetrievedChunk],
) -> None:
    _h, _e, client, kb_ver, retrieved = cache_env
    settings = get_settings()
    masked = str(mask("батареи холодные весь день").text)
    await store_cache(
        masked,
        problem_type="heating",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.9,
        chunk_ids=[str(retrieved.chunk_id)],
        model_name=settings.openrouter_model or "m",
        prompt_version=PROMPT_VERSION,
        kb_version=kb_ver,
        client=client,
    )
    miss = await lookup_cache(
        masked,
        model_name=settings.openrouter_model or "m",
        prompt_version="999-old",
        kb_version=kb_ver,
        threshold=settings.semantic_cache_threshold,
        client=client,
    )
    assert miss is None


async def test_invalidate_by_kb_version(
    session: AsyncSession,
    cache_env: tuple[uuid.UUID, uuid.UUID, AsyncQdrantClient, str, RetrievedChunk],
) -> None:
    _h, _e, client, kb_ver, retrieved = cache_env
    settings = get_settings()
    masked = str(mask("лифт снова не едет с утра").text)
    await store_cache(
        masked,
        problem_type="elevator",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.92,
        chunk_ids=[str(retrieved.chunk_id)],
        model_name=settings.openrouter_model or "m",
        prompt_version=PROMPT_VERSION,
        kb_version=kb_ver,
        client=client,
    )
    miss = await lookup_cache(
        masked,
        model_name=settings.openrouter_model or "m",
        prompt_version=PROMPT_VERSION,
        kb_version="deadbeefdeadbeef",
        threshold=settings.semantic_cache_threshold,
        client=client,
    )
    assert miss is None


async def test_payload_has_no_text_or_addressee(
    session: AsyncSession,
    cache_env: tuple[uuid.UUID, uuid.UUID, AsyncQdrantClient, str, RetrievedChunk],
) -> None:
    _h, _e, client, kb_ver, retrieved = cache_env
    settings = get_settings()
    masked = str(mask("засор канализации в подвале").text)
    await store_cache(
        masked,
        problem_type="sewage",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.93,
        chunk_ids=[str(retrieved.chunk_id)],
        model_name=settings.openrouter_model or "m",
        prompt_version=PROMPT_VERSION,
        kb_version=kb_ver,
        client=client,
    )
    points, _ = await client.scroll(collection_name=CACHE_COLLECTION, limit=20, with_payload=True)
    assert points
    for p in points:
        keys = set((p.payload or {}).keys())
        assert "message_masked" not in keys
        assert "text" not in keys
        assert "raw_text" not in keys
        assert "ads_phone" not in keys
        assert "contact" not in keys
        assert "house_id" not in keys
        assert "management_company_id" not in keys


async def test_low_confidence_not_cached(
    session: AsyncSession,
    cache_env: tuple[uuid.UUID, uuid.UUID, AsyncQdrantClient, str, RetrievedChunk],
) -> None:
    house_id, event_id, client, _kb, retrieved = cache_env
    settings = get_settings()
    search_fn = await _search_fixed(retrieved)
    llm = FakeLlmClient(
        responses=[
            LlmClassification(
                problem_type="cold_water",
                responsibility_zone=ResponsibilityZone.UK,
                confidence=0.5,
                cited_fragments=[1],
                reasoning="неуверен",
                clarifying_question="Где течёт?",
                clarifying_options=["В квартире", "На стояке"],
            )
        ]
    )
    result = await classify(
        "что-то с водой непонятное",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=search_fn,  # type: ignore[arg-type]
        settings=settings,
    )
    assert result.branch is Branch.CLARIFY
    count = await client.count(collection_name=CACHE_COLLECTION)
    assert count.count == 0


async def test_clarification_pass_not_cached(
    session: AsyncSession,
    cache_env: tuple[uuid.UUID, uuid.UUID, AsyncQdrantClient, str, RetrievedChunk],
) -> None:
    house_id, event_id, client, _kb, retrieved = cache_env
    settings = get_settings()
    search_fn = await _search_fixed(retrieved)
    llm = FakeLlmClient(responses=[_llm_auto(retrieved.ref)])
    await classify(
        "капает вода",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=search_fn,  # type: ignore[arg-type]
        settings=settings,
        clarification=Clarification(question="Где?", answer="На стояке"),
    )
    count = await client.count(collection_name=CACHE_COLLECTION)
    assert count.count == 0


async def test_fallback_model_not_cached(
    session: AsyncSession,
    cache_env: tuple[uuid.UUID, uuid.UUID, AsyncQdrantClient, str, RetrievedChunk],
) -> None:
    house_id, event_id, client, _kb, retrieved = cache_env
    settings = get_settings()
    search_fn = await _search_fixed(retrieved)

    class FallbackFake(FakeLlmClient):
        async def complete_json(self, *a: object, **k: object):  # type: ignore[no-untyped-def]
            result = await super().complete_json(*a, **k)  # type: ignore[arg-type]
            from dataclasses import replace

            return replace(result, fallback_model_used=True)

    llm = FallbackFake(responses=[_llm_auto(retrieved.ref)])
    result = await classify(
        "нет холодной воды совсем",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=search_fn,  # type: ignore[arg-type]
        settings=settings,
    )
    assert result.branch is Branch.AUTO
    # Ответ резервной модели — всё ещё LLM: fallback_used только для «без LLM».
    assert result.fallback_used is False
    count = await client.count(collection_name=CACHE_COLLECTION)
    assert count.count == 0
