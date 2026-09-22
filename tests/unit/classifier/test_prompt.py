"""Состав промпта CLASSIFY-001: MaskedText, таксономия, без EVAL-001."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from upravdom.classifier.prompt import (
    _SYNTHETIC_FEW_SHOT,
    PROMPT_VERSION,
    ProblemTypeInfo,
    build_messages,
)
from upravdom.classifier.schema import Clarification
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.masking import MaskedText
from upravdom.models.enums import ResponsibilityZone

# Путь набора EVAL-001 — architecture.md §13.1.
_EVAL_DIR = Path(__file__).resolve().parents[3] / "data" / "test_cases"


def _types() -> list[ProblemTypeInfo]:
    return [
        ProblemTypeInfo(
            code="cold_water",
            title="ХВС",
            default_responsibility_zone=ResponsibilityZone.UK,
            norm_reference="ПП №354",
        ),
        ProblemTypeInfo(
            code="other",
            title="Другое",
            default_responsibility_zone=ResponsibilityZone.UNKNOWN,
            norm_reference="—",
        ),
    ]


def test_prompt_version_is_stable() -> None:
    assert PROMPT_VERSION == "2"


def test_build_messages_accepts_only_masked_text() -> None:
    chunks = [
        RetrievedChunk(
            chunk_id=uuid.uuid4(),
            ref="п. 5 абз. 1",
            text="ПП РФ №491, п. 5 абз. 1\nграница ответственности",
            score=0.8,
        )
    ]
    messages = build_messages(
        MaskedText("Капает у [ФИО], тел. [ТЕЛЕФОН]"),
        chunks=chunks,
        problem_types=_types(),
    )
    joined = "\n".join(m.content for m in messages)
    assert "[ФИО]" in joined
    assert "[ТЕЛЕФОН]" in joined
    assert "cold_water" in joined
    # Фрагмент подписан номером и полной ссылкой с документом, без служебного `ref=`.
    assert "[1] ПП РФ №491, п. 5 абз. 1" in joined
    assert "ref=" not in joined
    assert "первое отключающее" in joined
    assert "сочинённый пример" in joined.lower() or "[сочинённый пример]" in joined


def test_raw_str_rejected_by_type_checkers() -> None:
    """Контракт: build_messages требует MaskedText — сырой str ловит mypy.

    В рантайме NewType не проверяется; фиксируем сигнатуру через аннотации.
    """

    hints = build_messages.__annotations__
    assert hints["masked"] is MaskedText or "MaskedText" in str(hints["masked"])


def test_clarification_appended() -> None:
    messages = build_messages(
        MaskedText("течёт"),
        chunks=[],
        problem_types=_types(),
        clarification=Clarification(question="Где?", answer="На стояке"),
    )
    user = messages[-1].content
    assert "Где?" in user
    assert "На стояке" in user
    assert "второй проход" in user


def test_few_shot_examples_are_marked_synthetic() -> None:
    for text, _hint in _SYNTHETIC_FEW_SHOT:
        assert "[сочинённый пример]" in text


def test_few_shot_not_from_eval_set() -> None:
    if not _EVAL_DIR.is_dir():
        pytest.skip("набор EVAL-001 (data/test_cases/) ещё не собран — проверять не с чем")
    eval_blobs = [
        path.read_text(encoding="utf-8").lower()
        for path in _EVAL_DIR.rglob("*")
        if path.suffix.lower() in {".json", ".jsonl", ".csv", ".txt", ".md"}
    ]
    for text, _hint in _SYNTHETIC_FEW_SHOT:
        body = text.replace("[сочинённый пример]", "").strip().lower()
        assert all(body not in blob for blob in eval_blobs)


def test_mypy_guard_import() -> None:
    # Импорт схемы для проверки, что LlmClassification доступен промпту/сервису.
    from upravdom.classifier.schema import LlmClassification

    assert "problem_type" in LlmClassification.model_fields
