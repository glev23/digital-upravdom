"""Ограничение частоты сообщений на пользователя (architecture.md §10).

In-memory, скользящее окно 60 секунд. Осознанно не в БД и не в Redis:
MVP — монолитный `App` в одном экземпляре (architecture.md §16, шаг 1
лестницы масштабирования); при переходе на несколько реплик это состояние
придётся вынести в общее хранилище — момент того перехода описан там же,
не раньше.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    """Не потокобезопасен намеренно: приложение — однопроцессный asyncio."""

    def __init__(self, *, max_per_minute: int) -> None:
        self._max_per_minute = max_per_minute
        self._history: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """True — можно обрабатывать; False — лимит исчерпан за последние 60с."""

        now = time.monotonic()
        window_start = now - 60.0
        history = self._history[key]
        while history and history[0] < window_start:
            history.popleft()

        if len(history) >= self._max_per_minute:
            return False

        history.append(now)
        return True
