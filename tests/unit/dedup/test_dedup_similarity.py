"""Сравнение описаний — без БД и без сети (DEDUP-001)."""

from __future__ import annotations

import pytest

from upravdom.dedup.similarity import cosine


def test_identical_vectors_are_one() -> None:
    assert cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_orthogonal_vectors_are_zero() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_scale_does_not_matter() -> None:
    assert cosine([3.0, 4.0], [6.0, 8.0]) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("left", "right"),
    [([], []), ([], [1.0]), ([1.0, 2.0], [1.0]), ([0.0, 0.0], [1.0, 1.0])],
)
def test_degenerate_input_is_zero_not_exception(left: list[float], right: list[float]) -> None:
    """Сбой сравнения не должен ронять основной сценарий (architecture.md §6.5)."""

    assert cosine(left, right) == 0.0
