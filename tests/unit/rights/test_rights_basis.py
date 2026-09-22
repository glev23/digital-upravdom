"""Проверка основания ответа (RIGHTS-001): что считается выдуманной нормой."""

from __future__ import annotations

import uuid

import pytest

from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.rights.service import mentions_unknown_norm

_SHOWN = [
    RetrievedChunk(
        chunk_id=uuid.uuid4(),
        ref="Прил. 1 п. 4",
        text="ПП РФ №354, Прил. 1 п. 4\nДопустимая продолжительность перерыва.",
        score=0.9,
    ),
    RetrievedChunk(
        chunk_id=uuid.uuid4(),
        ref="п. 13",
        text="ПП РФ №416, п. 13\nСроки локализации и устранения аварии.",
        score=0.8,
    ),
]


@pytest.mark.parametrize(
    "answer",
    [
        "Горячую воду могут отключать не дольше 8 часов суммарно за месяц.",
        "Аварию обязаны устранить за 3 суток, а локализовать за полчаса.",
        "Согласно ПП №354 перерыв ограничен.",
        "Это следует из п. 13 и приложения 1.",
        "За 10 рабочих дней вас обязаны предупредить.",
    ],
)
def test_answer_without_foreign_norms_is_allowed(answer: str) -> None:
    assert mentions_unknown_norm(answer, _SHOWN) is False


@pytest.mark.parametrize(
    "answer",
    [
        "Согласно ПП №290 управляющая компания обязана мыть подъезд.",
        "Это прямо указано в п. 27 правил.",
        "См. пункт 31 постановления.",
        "По ПП РФ № 731 информация раскрывается.",
    ],
)
def test_norm_outside_shown_fragments_is_refused(answer: str) -> None:
    assert mentions_unknown_norm(answer, _SHOWN) is True


def test_without_confirmed_fragments_any_norm_is_foreign() -> None:
    assert mentions_unknown_norm("По ПП №354 это так.", []) is True
