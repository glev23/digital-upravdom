"""In-memory лимитер частоты — architecture.md §10."""

from __future__ import annotations

import time

import pytest

from upravdom.bot_gateway.rate_limit import RateLimiter


def test_allows_up_to_limit() -> None:
    limiter = RateLimiter(max_per_minute=3)

    results = [limiter.allow("user-1") for _ in range(3)]

    assert results == [True, True, True]


def test_rejects_over_limit() -> None:
    limiter = RateLimiter(max_per_minute=2)

    results = [limiter.allow("user-1") for _ in range(3)]

    assert results == [True, True, False]


def test_different_users_have_independent_limits() -> None:
    limiter = RateLimiter(max_per_minute=1)

    assert limiter.allow("user-1") is True
    assert limiter.allow("user-2") is True
    assert limiter.allow("user-1") is False


def test_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = RateLimiter(max_per_minute=1)
    base = time.monotonic()
    times = iter([base, base + 61.0])

    # `time` — общий объект модуля stdlib: патчим его напрямую, а не через
    # `rate_limit_module.time` (mypy strict запрещает неявный реэкспорт).
    monkeypatch.setattr(time, "monotonic", lambda: next(times))

    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is True  # окно сдвинулось на 61с — лимит сброшен
