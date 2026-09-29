"""Строка «Основание» в ответе жителю (FLOW-001)."""

from __future__ import annotations

import uuid

from upravdom.classifier.schema import Branch, ClassificationResult
from upravdom.flow.handlers import _basis_line
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.models.enums import ResponsibilityZone
from upravdom.tickets.due import norm_matches

# Дословные начала пунктов (как выдаёт чанкер: первая строка — полная ссылка).
ZHK_161_2_3 = RetrievedChunk(
    chunk_id=uuid.uuid4(),
    ref="ст. 161 ч. 2.3",
    text=(
        "ЖК РФ, ст. 161 ч. 2.3\n"
        "2.3. При управлении многоквартирным домом управляющей организацией она несет "
        "ответственность перед собственниками помещений в многоквартирном доме за оказание "
        "всех услуг и (или) выполнение работ, которые обеспечивают надлежащее содержание "
        "общего имущества в данном доме."
    ),
    score=0.9,
    source_key="zhk",
)
PP491_P2 = RetrievedChunk(
    chunk_id=uuid.uuid4(),
    ref="п. 2",
    text=(
        "ПП РФ №491, п. 2\n"
        "2. В состав общего имущества включаются: б) крыши; в) ограждающие несущие "
        "конструкции многоквартирного дома."
    ),
    score=0.8,
    source_key="pp491",
)
ROOF_NORM = "Кровля — общее имущество МКД (ПП РФ №491, п.2). Числового норматива срока нет."


def _result(citations: list[RetrievedChunk]) -> ClassificationResult:
    return ClassificationResult(
        problem_type="roof_leak",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.8,
        branch=Branch.AUTO,
        citations=citations,
        fallback_used=False,
    )


def test_norm_matches_ignores_spacing_and_accepts_narrower_ref() -> None:
    assert norm_matches("ПП РФ №491, п. 2", ROOF_NORM)
    assert norm_matches("ПП РФ №491, п. 2 абз. 1", ROOF_NORM)
    assert not norm_matches("ЖК РФ, ст. 161 ч. 2.3", ROOF_NORM)
    # Похожие числа другого пункта совпадением не считаются.
    cold = "ПП РФ №354, приложение 1, п.1: допустимый перерыв — 4 часа."
    assert not norm_matches("ПП РФ №354, Прил. 1 п. 11", cold)


def test_basis_prefers_norm_from_problem_types() -> None:
    """Норма справочника объясняет зону; общая статья ЖК — нет.

    На живом прогоне жалоба на протечку потолка получила «Отвечает: УК» и
    основанием — ст. 161 ч. 2.3 про ответственность перед собственниками.
    """

    line, chunk_id = _basis_line(
        _result([ZHK_161_2_3, PP491_P2]),
        ROOF_NORM,
        type_title="Протечка кровли",
        raw_text="а кто отвечает за то что потолок протек и меня затопило",
    )
    assert line.startswith("Основание: ПП РФ №491, п. 2 — ")
    assert "крыши" in line
    # id пункта — для кнопки «Показать норму» с полным текстом.
    assert chunk_id == PP491_P2.chunk_id


def test_basis_falls_back_to_confirmed_citation() -> None:
    line, chunk_id = _basis_line(
        _result([ZHK_161_2_3]),
        ROOF_NORM,
        type_title="Протечка кровли",
        raw_text="потолок протек",
    )
    assert line.startswith("Основание: ЖК РФ, ст. 161 ч. 2.3 — ")
    assert chunk_id == ZHK_161_2_3.chunk_id


def test_basis_quote_is_short() -> None:
    """Живой прогон 28.09.2026: строка основания занимала 300–450 символов."""

    line, _ = _basis_line(
        _result([ZHK_161_2_3]),
        ROOF_NORM,
        type_title="Протечка кровли",
        raw_text="потолок протек",
    )
    quote = line.split(" — ", 1)[1]
    assert len(quote) <= 180
    assert not quote.startswith("…")


def test_basis_without_citations_shows_bare_norm() -> None:
    # Без подтверждённого фрагмента кнопки нет: показывать по ней нечего.
    assert _basis_line(_result([]), ROOF_NORM, type_title="Протечка кровли", raw_text="течёт") == (
        "Основание: ПП РФ №491, п.2",
        None,
    )
