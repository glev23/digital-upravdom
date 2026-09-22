"""Прототипы CLASSIFY-002: голосование, потолок, нет пересечения с EVAL."""

from __future__ import annotations

from pathlib import Path

import pytest

from upravdom.classifier.prototypes import (
    classify_by_prototypes,
    clear_prototypes_cache,
    prototype_texts,
)
from upravdom.config import get_settings

_TEST_CASES = Path(__file__).resolve().parents[3] / "data" / "test_cases"


def setup_function() -> None:
    clear_prototypes_cache()


def test_confidence_capped_below_high() -> None:
    high = get_settings().classify_confidence_high
    decision = classify_by_prototypes(
        "из крана на кухне вторые сутки тонкая струйка холодной",
        high=high,
    )
    assert decision.confidence < high
    assert decision.confidence <= high - 0.01 + 1e-9
    assert decision.problem_type == "cold_water"


def test_owner_boundary_prototype() -> None:
    high = get_settings().classify_confidence_high
    decision = classify_by_prototypes(
        "гибкая подводка к стиральной машине лопнула в санузле",
        high=high,
    )
    assert decision.problem_type == "cold_water"
    assert decision.responsibility_zone.value == "owner"
    assert decision.confidence < high


def test_no_overlap_with_eval_test_cases() -> None:
    if not _TEST_CASES.is_dir():
        pytest.skip("набор EVAL-001 (data/test_cases/) ещё не собран — проверять не с чем")
    prototypes = [t.strip().lower() for t in prototype_texts()]
    eval_blobs = [
        path.read_text(encoding="utf-8").lower()
        for path in _TEST_CASES.rglob("*")
        if path.suffix.lower() in {".json", ".jsonl", ".csv", ".txt", ".md"}
    ]
    overlap = [p for p in prototypes if any(p in blob for blob in eval_blobs)]
    assert not overlap, f"пересечение с EVAL: {overlap}"


def test_prototypes_cover_every_routable_zone() -> None:
    """Без примеров rso/municipality режим без LLM не мог назвать эти зоны вовсе."""

    import json

    from upravdom.classifier.prototypes import PROTOTYPES_JSON

    zones = {
        r["responsibility_zone"] for r in json.loads(PROTOTYPES_JSON.read_text(encoding="utf-8"))
    }
    assert {"uk", "rso", "owner", "municipality"} <= zones
