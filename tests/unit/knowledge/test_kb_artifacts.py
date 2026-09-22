"""Согласованность артефактов data/kb и выборочная сверка с embed()."""

from __future__ import annotations

import pytest

from upravdom.embeddings import PROMPT_SEARCH_DOCUMENT, embed
from upravdom.knowledge.artifacts import (
    CHUNKS_PATH,
    MANIFEST_PATH,
    VECTORS_PATH,
    load_chunks,
    load_manifest,
    load_vectors,
)

pytestmark = pytest.mark.skipif(
    not MANIFEST_PATH.exists(), reason="data/kb ещё не собран — scripts/build_kb.py"
)


def test_manifest_matches_chunks_and_vectors() -> None:
    manifest = load_manifest()
    chunks = load_chunks()
    vectors = load_vectors()
    assert manifest["count"] == len(chunks) == vectors.shape[0]
    assert vectors.shape[1] == manifest["dim"] == 768
    assert manifest["prompt_document"] == PROMPT_SEARCH_DOCUMENT


def test_sample_vectors_match_embed() -> None:
    chunks = load_chunks()
    vectors = load_vectors()
    # Первые, середина, последний — ловит устаревшие векторы после смены модели
    indices = sorted({0, len(chunks) // 2, len(chunks) - 1})
    texts = [chunks[i].chunk_text for i in indices]
    recomputed = embed(texts, prompt=PROMPT_SEARCH_DOCUMENT)
    for row, i in enumerate(indices):
        sim = float(recomputed[row] @ vectors[i])
        assert sim >= 0.9999, f"chunk {i}: cosine={sim}"


def test_artifact_files_exist() -> None:
    assert CHUNKS_PATH.is_file()
    assert VECTORS_PATH.is_file()
    assert MANIFEST_PATH.is_file()
