"""Сценарии 1–10 RIGHTS-001 на реальном Postgres и `FakeLlmClient`.

Поиск по базе знаний подменяется фикстурой: проверяется поведение вокруг
основания ответа (подтверждение ссылок, отказы, журнал), а не качество
retrieval — оно закрыто KB-001 (`scripts/kb_smoke.py`).
"""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest
from scripts.seed_demo import seed, stable_id
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import inbox
from upravdom.bot_gateway.inbox import ClaimedEvent, get_or_create_user
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.classifier.llm.fake import FakeLlmClient, timeout
from upravdom.classifier.schema import LlmClassification
from upravdom.config import get_settings
from upravdom.flow import texts as flow_texts
from upravdom.flow.handlers import on_message
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.models import RightsLog, Ticket
from upravdom.models.enums import RefusalReason, ResponsibilityZone
from upravdom.onboarding import service as onboarding_service
from upravdom.rights import texts as rights_texts
from upravdom.rights.schema import LlmRightsAnswer

pytestmark = pytest.mark.asyncio

_ts = itertools.count(1_758_590_000_000, 1000)
HOUSE_A = "house:house-dekabristov-10"

# Фрагменты, которые «нашёл» retrieval: первая строка — полная ссылка, как её
# кладёт чанкер KB-001.
_CHUNKS = [
    RetrievedChunk(
        chunk_id=uuid.uuid4(),
        ref="Прил. 1 п. 4",
        text=(
            "ПП РФ №354, Прил. 1 п. 4\n"
            "Допустимая продолжительность перерыва подачи горячей воды: "
            "8 часов суммарно в течение месяца."
        ),
        score=0.9,
        source_key="pp354",
    ),
    RetrievedChunk(
        chunk_id=uuid.uuid4(),
        ref="п. 31 пп. н",
        text=(
            "ПП РФ №354, п. 31 пп. н\n"
            "Исполнитель обязан предупредить потребителя о перерыве "
            "не позднее чем за 10 рабочих дней."
        ),
        score=0.8,
        source_key="pp354",
    ),
]


@pytest.fixture
def patch_search(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _search(*_a: object, **_k: object) -> list[RetrievedChunk]:
        return list(_CHUNKS)

    monkeypatch.setattr("upravdom.rights.service.search", _search)


@pytest.fixture
def patch_search_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _search(*_a: object, **_k: object) -> list[RetrievedChunk]:
        return []

    monkeypatch.setattr("upravdom.rights.service.search", _search)


@pytest.fixture
def patch_search_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _search(*_a: object, **_k: object) -> list[RetrievedChunk]:
        msg = "qdrant недоступен"
        raise ConnectionError(msg)

    monkeypatch.setattr("upravdom.rights.service.search", _search)


@pytest.fixture
def patch_classifier_search(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _search(*_a: object, **_k: object) -> list[RetrievedChunk]:
        return list(_CHUNKS)

    monkeypatch.setattr("upravdom.classifier.service.search", _search)


@pytest.fixture
async def resident(session: AsyncSession) -> str:
    await seed(session)
    for row in _problem_types():
        await session.execute(_UPSERT_PT, row)
    max_user_id = f"rgt-{uuid.uuid4().hex[:10]}"
    user = await get_or_create_user(session, max_user_id)
    await onboarding_service.bind_house(session, user.id, stable_id(HOUSE_A))
    await onboarding_service.grant_consent(
        session, user.id, version=get_settings().consent_version, source="test"
    )
    await session.commit()
    return max_user_id


_UPSERT_PT = text(
    """
    INSERT INTO problem_types
        (code, title, default_responsibility_zone, resolution_hours, norm_reference)
    VALUES
        (:code, :title, :default_responsibility_zone, :resolution_hours, :norm_reference)
    ON CONFLICT (code) DO NOTHING
    """
)


def _problem_types() -> list[dict[str, Any]]:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "data" / "seed" / "problem_types.json"
    return list(json.loads(path.read_text(encoding="utf-8")))


def _message(user: str, body: str) -> dict[str, Any]:
    return {
        "update_type": "message_created",
        "timestamp": next(_ts),
        "message": {
            "sender": {"user_id": user},
            "recipient": {"chat_id": user},
            "body": {"mid": f"mid-{uuid.uuid4()}", "text": body},
        },
    }


async def _say(session: AsyncSession, user: str, body: str, llm: FakeLlmClient) -> uuid.UUID:
    payload = _message(user, body)
    parsed = parse_webhook_payload(payload)
    await inbox.record_inbound_event(
        session, max_event_id=parsed.event_id, payload=payload, max_user_id=parsed.max_user_id
    )
    event_id = await session.scalar(
        text(
            "UPDATE inbound_events SET status = 'processing', attempts = 1 "
            "WHERE max_event_id = :m RETURNING id"
        ),
        {"m": parsed.event_id},
    )
    assert event_id is not None
    await on_message(session, ClaimedEvent(event_id, parsed.event_id, payload, 1), llm=llm)
    await inbox.mark_done(session, event_id)
    return uuid.UUID(str(event_id))


async def _replies(session: AsyncSession, chat: str) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT payload FROM outbound_messages WHERE max_chat_id = :c "
            "AND NOT (payload::jsonb ? 'callback_id') ORDER BY created_at, id"
        ),
        {"c": chat},
    )
    return [row[0]["text"] for row in rows]


async def _log(session: AsyncSession) -> RightsLog:
    return (await session.execute(select(RightsLog))).scalars().one()


def _answer(
    *,
    sufficient: bool = True,
    answer: str = "Горячую воду могут отключать не дольше установленного норматива.",
    cited: list[int] | None = None,
) -> LlmRightsAnswer:
    return LlmRightsAnswer(
        sufficient=sufficient,
        answer=answer,
        cited_fragments=[1] if cited is None else cited,
    )


# --- 1, 2 ---------------------------------------------------------------------


async def test_answer_cites_confirmed_norm(
    session: AsyncSession, resident: str, patch_search: None
) -> None:
    llm = FakeLlmClient(responses=[_answer(cited=[1])])

    await _say(session, resident, "могут ли отключить горячую воду на месяц?", llm)

    replies = await _replies(session, resident)
    assert rights_texts.ACK_SEARCHING in replies
    assert flow_texts.ACK_RECEIVED not in replies
    body = replies[-1]
    assert "ПП РФ №354, Прил. 1 п. 4" in body
    assert rights_texts.DISCLAIMER in body
    assert (await _log(session)).refused is False
    assert (await session.scalar(select(Ticket))) is None  # заявка не создаётся


async def test_command_answer_lists_several_bases(
    session: AsyncSession, resident: str, patch_search: None
) -> None:
    llm = FakeLlmClient(responses=[_answer(cited=[1, 2])])

    await _say(
        session, resident, "/права за сколько должны предупредить о плановом отключении?", llm
    )

    body = (await _replies(session, resident))[-1]
    assert "• ПП РФ №354, Прил. 1 п. 4" in body
    assert "• ПП РФ №354, п. 31 пп. н" in body


# --- 3, 4 ---------------------------------------------------------------------


async def test_who_question_goes_to_classification(
    session: AsyncSession, resident: str, patch_classifier_search: None
) -> None:
    llm = FakeLlmClient(
        responses=[
            LlmClassification(
                problem_type="cold_water",
                responsibility_zone=ResponsibilityZone.UK,
                confidence=0.95,
                cited_fragments=[1],
                reasoning="тест",
            )
        ]
    )

    await _say(session, resident, "кто должен чинить стояк?", llm)

    replies = await _replies(session, resident)
    assert flow_texts.ACK_RECEIVED in replies
    assert rights_texts.ACK_SEARCHING not in replies
    assert (await session.scalar(select(Ticket))) is not None
    assert (await session.execute(select(RightsLog))).scalars().all() == []


async def test_problem_description_with_question_mark_goes_to_classification(
    session: AsyncSession, resident: str, patch_classifier_search: None
) -> None:
    llm = FakeLlmClient(
        responses=[
            LlmClassification(
                problem_type="cold_water",
                responsibility_zone=ResponsibilityZone.UK,
                confidence=0.95,
                cited_fragments=[1],
                reasoning="тест",
            )
        ]
    )

    await _say(session, resident, "опять нет воды?", llm)

    assert rights_texts.ACK_SEARCHING not in await _replies(session, resident)
    assert (await session.scalar(select(Ticket))) is not None


# --- 5, 6, 7 ------------------------------------------------------------------


async def test_insufficient_basis_is_refused(
    session: AsyncSession, resident: str, patch_search: None
) -> None:
    llm = FakeLlmClient(responses=[_answer(sufficient=False, answer="", cited=[])])

    await _say(session, resident, "положен ли мне новый балкон за счёт УК?", llm)

    body = (await _replies(session, resident))[-1]
    assert "прямого ответа на этот вопрос нет" in body
    assert "+7 (843) 000-00-01" in body  # АДС УК дома
    log = await _log(session)
    assert log.refused is True
    assert log.refusal_reason is RefusalReason.INSUFFICIENT


async def test_citation_to_missing_fragment_is_refused(
    session: AsyncSession, resident: str, patch_search: None
) -> None:
    """Модель сослалась на фрагмент, которого ей не показывали."""

    llm = FakeLlmClient(responses=[_answer(cited=[7, 9])])

    await _say(session, resident, "какой срок устранения аварии?", llm)

    assert "прямого ответа на этот вопрос нет" in (await _replies(session, resident))[-1]
    log = await _log(session)
    assert log.refused is True
    assert log.refusal_reason is RefusalReason.NO_CITATIONS


async def test_unknown_norm_in_answer_text_is_refused(
    session: AsyncSession, resident: str, patch_search: None
) -> None:
    """Даже с подтверждённым фрагментом: в тексте норма, которую не показывали."""

    llm = FakeLlmClient(
        responses=[_answer(answer="Согласно ПП №290 управляющая компания обязана.", cited=[1])]
    )

    await _say(session, resident, "обязаны ли мыть подъезд?", llm)

    body = (await _replies(session, resident))[-1]
    assert "прямого ответа на этот вопрос нет" in body
    assert "290" not in body
    log = await _log(session)
    assert log.refusal_reason is RefusalReason.UNKNOWN_NORM_IN_ANSWER


# --- 8 ------------------------------------------------------------------------


async def test_llm_unavailable_is_honest(
    session: AsyncSession, resident: str, patch_search: None
) -> None:
    llm = FakeLlmClient(responses=[timeout()])

    await _say(session, resident, "могут ли отключить воду без предупреждения?", llm)

    body = (await _replies(session, resident))[-1]
    assert "не могу подобрать ответ по нормативам" in body
    assert "+7 (843) 000-00-01" in body
    assert (await _log(session)).refusal_reason is RefusalReason.LLM_UNAVAILABLE


async def test_kb_unavailable_does_not_call_llm(
    session: AsyncSession, resident: str, patch_search_broken: None
) -> None:
    llm = FakeLlmClient(responses=[_answer()])

    await _say(session, resident, "какой срок устранения аварии?", llm)

    assert llm.calls == []
    assert "не могу подобрать ответ по нормативам" in (await _replies(session, resident))[-1]
    assert (await _log(session)).refusal_reason is RefusalReason.KB_UNAVAILABLE


async def test_empty_search_is_refused_without_llm(
    session: AsyncSession, resident: str, patch_search_empty: None
) -> None:
    llm = FakeLlmClient(responses=[_answer()])

    await _say(session, resident, "положен ли перерасчёт за лифт?", llm)

    assert llm.calls == []
    assert (await _log(session)).refusal_reason is RefusalReason.NO_CITATIONS


# --- 9 ------------------------------------------------------------------------


async def test_command_without_question_gets_a_hint(
    session: AsyncSession, resident: str, patch_search: None
) -> None:
    llm = FakeLlmClient(responses=[])

    await _say(session, resident, "/права", llm)

    replies = await _replies(session, resident)
    assert replies[-1] == rights_texts.HINT_NO_QUESTION
    assert rights_texts.ACK_SEARCHING not in replies
    assert llm.calls == []
    assert (await session.execute(select(RightsLog))).scalars().all() == []


# --- 10 -----------------------------------------------------------------------


async def test_personal_data_is_masked_in_llm_and_log(
    session: AsyncSession, resident: str, patch_search: None
) -> None:
    llm = FakeLlmClient(responses=[_answer(cited=[1])])
    question = "положен ли перерасчёт? я Иванова из кв. 42, телефон +7 917 123-45-67"

    await _say(session, resident, question, llm)

    prompt = llm.calls[0][-1].content
    for secret in ("Иванова", "42", "917"):
        assert secret not in prompt
    log = await _log(session)
    for secret in ("Иванова", "917"):
        assert secret not in log.question_masked
    assert "[ТЕЛЕФОН]" in log.question_masked


async def test_log_keeps_links_not_the_model_answer(
    session: AsyncSession, resident: str, patch_search: None
) -> None:
    """Текст ответа модели не хранится — он может пересказывать вопрос (§11)."""

    llm = FakeLlmClient(responses=[_answer(answer="Уникальный текст ответа модели.", cited=[1])])

    await _say(session, resident, "сколько часов можно без горячей воды?", llm)

    log = await _log(session)
    assert log.used_chunk_ids == [str(_CHUNKS[0].chunk_id)]
    assert log.model_name == "fake/model"
    assert log.prompt_version == "rights-1"
    assert log.latency_ms > 0
    dumped = " ".join(str(value) for value in vars(log).values())
    assert "Уникальный текст ответа модели." not in dumped
