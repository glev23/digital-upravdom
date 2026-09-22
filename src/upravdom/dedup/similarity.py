"""Похожесть двух описаний (DEDUP-001, architecture.md §6.5).

Отдельный модуль без БД и без сети: порог склейки калибруется
`scripts/run_dedup_check.py` ровно через эту функцию.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from upravdom.embeddings import PROMPT_CLASSIFICATION, embed


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """0.0 при пустом или нулевом векторе — не исключение: сравнение
    дедупликации не должно ронять основной сценарий (§6.5)."""

    if not len(left) or len(left) != len(right):
        return 0.0
    a = np.asarray(left, dtype=np.float32)
    b = np.asarray(right, dtype=np.float32)
    norms = float(np.linalg.norm(a)) * float(np.linalg.norm(b))
    if norms < 1e-9:
        return 0.0
    return float(np.dot(a, b) / norms)


def embed_for_dedup(masked: str) -> list[float]:
    """Эмбеддинг маскированного текста — тот же промпт, что у семантического
    кэша (CLASSIFY-002): имена и телефоны не делают одинаковые жалобы разными.

    Синхронная и тяжёлая: из async-кода вызывать через `asyncio.to_thread`.
    """

    vector: list[float] = embed([masked], prompt=PROMPT_CLASSIFICATION)[0].tolist()
    return vector


def embed_many_for_dedup(masked: list[str]) -> list[list[float]]:
    """Батч для офлайн-калибровки порога (`scripts/run_dedup_check.py`)."""

    if not masked:
        return []
    return [row.tolist() for row in embed(masked, prompt=PROMPT_CLASSIFICATION)]


__all__ = ["cosine", "embed_for_dedup", "embed_many_for_dedup"]
