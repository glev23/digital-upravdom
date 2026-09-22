"""Публичный API классификатора (CLASSIFY-001)."""

from __future__ import annotations

from upravdom.classifier.prompt import PROMPT_VERSION
from upravdom.classifier.schema import (
    Branch,
    Clarification,
    ClassificationResult,
    LlmClassification,
)
from upravdom.classifier.service import classify, confirm_citations, select_branch

__all__ = [
    "PROMPT_VERSION",
    "Branch",
    "ClassificationResult",
    "Clarification",
    "LlmClassification",
    "classify",
    "confirm_citations",
    "select_branch",
]
