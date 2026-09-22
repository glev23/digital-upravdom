"""Пороги и границы веток CLASSIFY-001."""

from __future__ import annotations

import uuid

from upravdom.classifier.schema import Branch
from upravdom.classifier.service import confirm_citations, select_branch
from upravdom.knowledge.retrieval import RetrievedChunk


def _chunk(ref: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        ref=ref,
        text=f"текст {ref}",
        score=0.9,
    )


def test_branch_auto_at_high_boundary() -> None:
    assert (
        select_branch(
            0.75,
            high=0.75,
            low=0.45,
            clarifying_question=None,
            clarifying_options=[],
            after_clarification=False,
        )
        is Branch.AUTO
    )


def test_branch_clarify_just_below_high() -> None:
    assert (
        select_branch(
            0.749,
            high=0.75,
            low=0.45,
            clarifying_question="Где течёт?",
            clarifying_options=["В квартире", "В подъезде"],
            after_clarification=False,
        )
        is Branch.CLARIFY
    )


def test_branch_clarify_at_low_boundary() -> None:
    assert (
        select_branch(
            0.45,
            high=0.75,
            low=0.45,
            clarifying_question="Где течёт?",
            clarifying_options=["В квартире", "На стояке", "Не знаю"],
            after_clarification=False,
        )
        is Branch.CLARIFY
    )


def test_branch_unknown_below_low() -> None:
    assert (
        select_branch(
            0.449,
            high=0.75,
            low=0.45,
            clarifying_question="Где течёт?",
            clarifying_options=["В квартире", "В подъезде"],
            after_clarification=False,
        )
        is Branch.UNKNOWN
    )


def test_mid_without_options_is_unknown() -> None:
    assert (
        select_branch(
            0.6,
            high=0.75,
            low=0.45,
            clarifying_question="Где течёт?",
            clarifying_options=["только один"],
            after_clarification=False,
        )
        is Branch.UNKNOWN
    )


def test_after_clarification_mid_becomes_unknown() -> None:
    assert (
        select_branch(
            0.6,
            high=0.75,
            low=0.45,
            clarifying_question="Ещё вопрос?",
            clarifying_options=["А", "Б"],
            after_clarification=True,
        )
        is Branch.UNKNOWN
    )


def test_after_clarification_high_still_auto() -> None:
    assert (
        select_branch(
            0.9,
            high=0.75,
            low=0.45,
            clarifying_question=None,
            clarifying_options=[],
            after_clarification=True,
        )
        is Branch.AUTO
    )


def test_confirm_citations_drops_nonexistent_fragments() -> None:
    chunks = [_chunk("ПП №491 п. 5"), _chunk("ПП №354 Прил. 1 п. 1")]
    confirmed = confirm_citations([1, 99, 0, -1, 1, 2], chunks)
    assert [c.ref for c in confirmed] == ["ПП №491 п. 5", "ПП №354 Прил. 1 п. 1"]


def test_confirm_citations_empty_when_nothing_shown() -> None:
    assert confirm_citations([1, 2], []) == []
