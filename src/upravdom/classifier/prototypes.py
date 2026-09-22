"""kNN по прототипам без LLM (CLASSIFY-002).

Векторы в памяти процесса — деградация не зависит от Qdrant.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from upravdom.embeddings import PROMPT_CLASSIFICATION, embed
from upravdom.models.enums import ResponsibilityZone

logger = logging.getLogger(__name__)

PROTOTYPES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "classifier"
PROTOTYPES_JSON = PROTOTYPES_DIR / "prototypes.json"
PROTOTYPES_NPY = PROTOTYPES_DIR / "prototypes.npy"
PROTOTYPES_MANIFEST = PROTOTYPES_DIR / "manifest.json"

K_DEFAULT = 5


@dataclass(slots=True, frozen=True)
class PrototypeExample:
    text: str
    problem_type: str
    responsibility_zone: ResponsibilityZone


@dataclass(slots=True, frozen=True)
class PrototypeDecision:
    problem_type: str
    responsibility_zone: ResponsibilityZone
    confidence: float
    runner_up_type: str | None
    best_score: float


@lru_cache
def _load_bundle() -> tuple[tuple[PrototypeExample, ...], NDArray[np.float32]]:
    rows = json.loads(PROTOTYPES_JSON.read_text(encoding="utf-8"))
    examples = tuple(
        PrototypeExample(
            text=str(r["text"]),
            problem_type=str(r["problem_type"]),
            responsibility_zone=ResponsibilityZone(str(r["responsibility_zone"])),
        )
        for r in rows
    )
    vectors = np.asarray(np.load(PROTOTYPES_NPY), dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[0] != len(examples):
        msg = f"prototypes.npy shape {vectors.shape} != {len(examples)} examples"
        raise ValueError(msg)
    return examples, vectors


def clear_prototypes_cache() -> None:
    _load_bundle.cache_clear()


def prototype_texts() -> list[str]:
    examples, _ = _load_bundle()
    return [e.text for e in examples]


def classify_by_prototypes(
    masked_text: str,
    *,
    high: float,
    k: int = K_DEFAULT,
) -> PrototypeDecision:
    """Взвешенное голосование top-k; confidence с потолком high − 0.01."""

    examples, matrix = _load_bundle()
    query = embed([masked_text], prompt=PROMPT_CLASSIFICATION)[0]
    scores = matrix @ query  # cosine: векторы L2-нормированы
    k_eff = min(k, len(examples))
    top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
    top_idx = top_idx[np.argsort(-scores[top_idx])]

    weights: dict[tuple[str, str], float] = {}
    type_weights: dict[str, float] = {}
    for idx in top_idx:
        ex = examples[int(idx)]
        w = float(max(scores[int(idx)], 0.0))
        key = (ex.problem_type, ex.responsibility_zone.value)
        weights[key] = weights.get(key, 0.0) + w
        type_weights[ex.problem_type] = type_weights.get(ex.problem_type, 0.0) + w

    winner_key = max(weights, key=weights.get)  # type: ignore[arg-type]
    total = sum(weights.values()) or 1.0
    best_score = float(scores[int(top_idx[0])])
    vote_share = weights[winner_key] / total
    confidence = min(vote_share * best_score, high - 0.01)

    sorted_types = sorted(type_weights.items(), key=lambda kv: kv[1], reverse=True)
    runner_up = sorted_types[1][0] if len(sorted_types) > 1 else None
    if runner_up == winner_key[0]:
        runner_up = next((t for t, _ in sorted_types if t != winner_key[0]), None)

    return PrototypeDecision(
        problem_type=winner_key[0],
        responsibility_zone=ResponsibilityZone(winner_key[1]),
        confidence=float(confidence),
        runner_up_type=runner_up,
        best_score=best_score,
    )
