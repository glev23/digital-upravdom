"""Консультант: отказы и проверки ответа (без сети и без базы)."""

from __future__ import annotations

import uuid

import pytest

from upravdom.classifier.llm.fake import FakeLlmClient, timeout
from upravdom.consult import service
from upravdom.consult.schema import LlmConsultAnswer

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_kb(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(*_a: object, **_k: object) -> list[object]:
        return []

    monkeypatch.setattr(service, "search", empty)


async def _ask(answer: LlmConsultAnswer | Exception) -> service.ConsultAnswer:
    return await service.answer_consult(
        "в подъезде орут по ночам",
        inbound_event_id=uuid.uuid4(),
        llm=FakeLlmClient(responses=[answer]),
    )


async def test_advice_is_returned() -> None:
    out = await _ask(
        LlmConsultAnswer(answer="Шумом занимается участковый. При угрозе жизни — 112.")
    )
    assert out.ok
    assert "участковый" in out.answer


async def test_needs_ticket_falls_back_to_ticket() -> None:
    """«Кот нагадил в подъезде» — уборка УК: бот оформляет заявку сам."""

    out = await _ask(LlmConsultAnswer(needs_ticket=True))
    assert not out.ok
    assert out.reason == "needs_ticket"


async def test_invented_norm_is_refused() -> None:
    # Ни одного фрагмента не показано — любой номер акта выдуман.
    out = await _ask(LlmConsultAnswer(answer="Шум запрещён ФЗ № 52, звоните участковому."))
    assert not out.ok
    assert out.reason == "unknown_norm"


@pytest.mark.parametrize(
    "answer",
    [
        "Позвоните в дежурную часть: 8 (843) 222-33-44.",
        "Подробности на сайте https://example.ru",
        "Пишите на www.gorod.ru",
    ],
)
async def test_invented_contacts_are_refused(answer: str) -> None:
    out = await _ask(LlmConsultAnswer(answer=answer))
    assert not out.ok
    assert out.reason == "contact_in_answer"


async def test_112_is_allowed() -> None:
    out = await _ask(LlmConsultAnswer(answer="Если есть угроза жизни — звоните 112."))
    assert out.ok


async def test_llm_failure_and_empty_answer_are_refusals() -> None:
    assert not (await _ask(timeout())).ok
    assert (await _ask(LlmConsultAnswer(answer=""))).reason == "empty"
