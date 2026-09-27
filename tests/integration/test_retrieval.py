"""Фильтры retrieval: редакция и УК."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.embeddings import PROMPT_SEARCH_DOCUMENT, embed
from upravdom.knowledge.artifacts import KB_COLLECTION
from upravdom.knowledge.loader import ensure_kb_collection, load_kb_from_artifacts
from upravdom.knowledge.retrieval import search
from upravdom.vector_store import get_qdrant_client

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def kb_loaded(session: AsyncSession) -> AsyncIterator[AsyncQdrantClient]:
    client = get_qdrant_client()
    names = {c.name for c in (await client.get_collections()).collections}
    if KB_COLLECTION in names:
        await client.delete_collection(KB_COLLECTION)
    await load_kb_from_artifacts(session, client)
    yield client


async def test_pp40_invisible_before_effective_date(kb_loaded: AsyncQdrantClient) -> None:
    hits = await search(
        "уведомления от УК в мессенджере MAX",
        k=5,
        on_date=date(2026, 8, 31),
        client=kb_loaded,
    )
    assert all(h.source_key != "pp40" for h in hits)


async def test_pp40_visible_on_effective_date(kb_loaded: AsyncQdrantClient) -> None:
    hits = await search(
        "уведомления от УК в мессенджере MAX",
        k=5,
        on_date=date(2026, 9, 1),
        client=kb_loaded,
    )
    assert any(h.source_key == "pp40" for h in hits)


async def test_company_filter_hides_other_uk_chunks(kb_loaded: AsyncQdrantClient) -> None:
    client = kb_loaded
    await ensure_kb_collection(client)
    uk_a = uuid.uuid4()
    uk_b = uuid.uuid4()
    vec = embed(["внутренний регламент УК про протечки"], prompt=PROMPT_SEARCH_DOCUMENT)[0].tolist()
    await client.upsert(
        collection_name=KB_COLLECTION,
        points=[
            qm.PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={
                    "chunk_id": str(uuid.uuid4()),
                    "document_id": str(uuid.uuid4()),
                    "source_key": "uk_local",
                    "ref": "регламент",
                    "chunk_text": "Внутренний регламент УК А: протечки стояков чинит УК.",
                    "effective_from": "2000-01-01",
                    "effective_to": None,
                    "region_code": None,
                    "management_company_id": str(uk_a),
                },
            )
        ],
    )
    hits_b = await search(
        "протечки стояков по регламенту УК",
        k=5,
        on_date=date(2026, 9, 22),
        management_company_id=uk_b,
        client=client,
    )
    assert all(h.source_key != "uk_local" for h in hits_b)

    hits_a = await search(
        "протечки стояков по регламенту УК",
        k=5,
        on_date=date(2026, 9, 22),
        management_company_id=uk_a,
        client=client,
    )
    assert any(h.source_key == "uk_local" for h in hits_a)


async def test_uk_document_visible_only_to_its_own_company(kb_loaded: AsyncQdrantClient) -> None:
    """Документ УК из `data/kb/sources` виден только жителям её домов (KB-002).

    Проверка на реальном артефакте, а не на подложенных точках: ошибка в
    front-matter или в загрузчике иначе осталась бы незамеченной.
    """

    from scripts.seed_demo import stable_id

    vahitovskaya = stable_id("mc:uk-vahitovskaya")
    privolzhskaya = stable_id("mc:uk-privolzhskaya")
    question = "за сколько часов приедет мастер после заявки об аварии"

    own = await search(question, k=5, management_company_id=vahitovskaya, client=kb_loaded)
    assert any(h.source_key == "uk_vahitovskaya" for h in own)
    # Подпись, которую увидит житель, честно называет документ тестовым.
    own_labels = [h.label for h in own if h.source_key == "uk_vahitovskaya"]
    assert all("тестовый документ" in label for label in own_labels)

    other = await search(question, k=5, management_company_id=privolzhskaya, client=kb_loaded)
    assert all(h.source_key != "uk_vahitovskaya" for h in other)

    # Без УК (дом без управляющей компании) — только федеральные нормы.
    federal = await search(question, k=5, client=kb_loaded)
    assert all(h.source_key != "uk_vahitovskaya" for h in federal)


async def test_uk_document_does_not_displace_federal_norms(kb_loaded: AsyncQdrantClient) -> None:
    """Слой УК — надстройка, а не замена: вопрос о нормативе остаётся у нормы."""

    from scripts.seed_demo import stable_id

    hits = await search(
        "за сколько дней должны предупредить о плановом отключении воды",
        k=5,
        management_company_id=stable_id("mc:uk-vahitovskaya"),
        client=kb_loaded,
    )
    assert hits[0].source_key == "pp354"
