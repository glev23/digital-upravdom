"""Интеграция classify(): FakeLlm + Postgres (+ опционально Qdrant)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scripts.seed_demo import seed, stable_id
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.classifier.llm.fake import FakeLlmClient, timeout
from upravdom.classifier.schema import Branch, Clarification, LlmClassification
from upravdom.classifier.service import classify
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.masking import PLACEHOLDER_PHONE
from upravdom.models import ClassificationLog
from upravdom.models.enums import ResponsibilityZone

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


async def _seed_problem_types(session: AsyncSession) -> None:
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
        {
            "id": event_id,
            "m": f"classify-{event_id}",
            "ts": datetime.now(UTC),
        },
    )
    return event_id


@pytest.fixture
async def classify_env(session: AsyncSession) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    await _seed_problem_types(session)
    await seed(session)
    house_id = stable_id("house:house-dekabristov-10")
    event_id = await _inbound(session)
    await session.commit()
    yield house_id, event_id


def _llm_ok(**overrides: object) -> LlmClassification:
    data = {
        "problem_type": "cold_water",
        "responsibility_zone": ResponsibilityZone.UK,
        "confidence": 0.9,
        "cited_fragments": [1],
        "reasoning": "сифон / стояк — общее имущество",
        "clarifying_question": None,
        "clarifying_options": [],
    }
    data.update(overrides)
    return LlmClassification.model_validate(data)


async def _fake_search(*_a: object, **_k: object) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=uuid.uuid4(),
            ref="ПП №491 п. 5",
            text="В состав общего имущества включаются сети до первого отключающего устройства.",
            score=0.95,
            source_key="pp491",
        )
    ]


async def test_auto_writes_masked_log(
    session: AsyncSession, classify_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    house_id, event_id = classify_env
    llm = FakeLlmClient(responses=[_llm_ok()])
    raw = "У Иванова нет воды, звоните +7 903 111-22-33"

    result = await classify(
        raw,
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=_fake_search,
    )

    assert result.branch is Branch.AUTO
    assert result.problem_type == "cold_water"
    assert result.log_id is not None
    assert result.model_name == "fake/model"
    assert len(llm.calls) == 1
    prompt_blob = "\n".join(m.content for m in llm.calls[0])
    assert "+7 903" not in prompt_blob
    assert PLACEHOLDER_PHONE in prompt_blob or "[ТЕЛЕФОН]" in prompt_blob
    assert "Иванов" not in prompt_blob or "[ФИО]" in prompt_blob

    row = (
        await session.execute(
            select(ClassificationLog).where(ClassificationLog.id == result.log_id)
        )
    ).scalar_one()
    assert PLACEHOLDER_PHONE in row.message_masked or "[ТЕЛЕФОН]" in row.message_masked
    assert "+7 903" not in row.message_masked
    assert row.prompt_version is not None
    assert row.model_name == "fake/model"
    assert row.cache_hit is False
    assert row.used_chunk_ids


async def test_invented_citation_blocks_auto(
    session: AsyncSession, classify_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    house_id, event_id = classify_env
    llm = FakeLlmClient(
        responses=[
            _llm_ok(cited_fragments=[99], confidence=0.99),
        ]
    )
    result = await classify(
        "из стены сифонит",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=_fake_search,
    )
    assert result.branch is not Branch.AUTO
    assert result.citations == []
    assert result.confidence < 0.75


async def test_unknown_problem_type_becomes_other(
    session: AsyncSession, classify_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    house_id, event_id = classify_env
    llm = FakeLlmClient(
        responses=[_llm_ok(problem_type="spaceship", confidence=0.99)],
    )
    result = await classify(
        "странный шум",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=_fake_search,
    )
    assert result.problem_type == "other"
    assert result.branch is Branch.UNKNOWN


async def test_llm_error_uses_prototypes(
    session: AsyncSession, classify_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    house_id, event_id = classify_env
    llm = FakeLlmClient(responses=[timeout()])
    result = await classify(
        "из крана на кухне вторые сутки тонкая струйка холодной",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=_fake_search,
    )
    assert result.fallback_used is True
    assert result.branch is not Branch.AUTO
    assert result.confidence < 0.75


async def test_qdrant_and_llm_down_still_prototypes(
    session: AsyncSession, classify_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    house_id, event_id = classify_env

    async def boom(*_a: object, **_k: object) -> list[RetrievedChunk]:
        raise RuntimeError("qdrant down")

    result = await classify(
        "батареи едва тёплые хотя на улице мороз",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=FakeLlmClient(responses=[timeout()]),
        search_fn=boom,
    )
    assert result.fallback_used is True
    assert result.branch is not Branch.AUTO
    assert result.problem_type in {
        "heating",
        "cold_water",
        "hot_water",
        "other",
        "common_area",
    }


async def test_force_no_llm(
    session: AsyncSession, classify_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    house_id, event_id = classify_env
    llm = FakeLlmClient(responses=[_llm_ok()])
    result = await classify(
        "кабина лифта застряла между этажами с людьми",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=_fake_search,
        force_no_llm=True,
    )
    assert llm.calls == []
    assert result.fallback_used is True
    assert result.branch is not Branch.AUTO


async def test_empty_text_skips_llm(
    session: AsyncSession, classify_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    house_id, event_id = classify_env
    llm = FakeLlmClient(responses=[_llm_ok()])
    result = await classify(
        "🔥🔥🔥",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=_fake_search,
    )
    assert result.branch is Branch.UNKNOWN
    assert llm.calls == []


async def test_clarification_second_pass_no_clarify_branch(
    session: AsyncSession, classify_env: tuple[uuid.UUID, uuid.UUID]
) -> None:
    house_id, event_id = classify_env
    llm = FakeLlmClient(
        responses=[
            _llm_ok(
                confidence=0.6,
                clarifying_question="Где течёт?",
                clarifying_options=["В квартире", "На стояке"],
            )
        ]
    )
    result = await classify(
        "капает",
        house_id=house_id,
        inbound_event_id=event_id,
        session=session,
        llm=llm,
        search_fn=_fake_search,
        clarification=Clarification(question="Где течёт?", answer="На стояке"),
    )
    assert result.branch is Branch.UNKNOWN
    assert result.clarifying_question is None
