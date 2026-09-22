"""Тесты ONNX-эмбеддингов (SPIKE-001). Реальная модель, без torch."""

from __future__ import annotations

import numpy as np

from upravdom.embeddings import embed


def test_embed_returns_normalized_768d_vectors() -> None:
    vectors = embed(["нет света в подъезде", "течёт с потолка"])

    assert vectors.shape == (2, 768)
    norms = np.linalg.norm(vectors, axis=1)
    np.testing.assert_allclose(norms, 1.0, atol=1e-5)


def test_embed_empty_list() -> None:
    vectors = embed([])

    assert vectors.shape == (0, 768)


def test_embed_similar_texts_closer_than_unrelated() -> None:
    vectors = embed(
        [
            "сифонит вода из стены за ванной",
            "течёт вода из стены рядом с ванной",
            "не убирают снег во дворе",
        ]
    )
    sim_related = float(vectors[0] @ vectors[1])
    sim_unrelated = float(vectors[0] @ vectors[2])

    assert sim_related > sim_unrelated
