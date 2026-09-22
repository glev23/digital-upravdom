"""Формат callback-кнопок дедупликации (DEDUP-001).

`ddp:split:<номер merged-заявки>` — новый префикс рядом с `onb:` и `clf:`
(маршрутизация в `bot_gateway/dispatcher.py:route_callback`).

В payload только номер: кириллица и свободный текст туда не кладутся (лимит
1024 символа, урок ONBOARD-002). Номер подделывается тривиально, поэтому при
нажатии права перепроверяются в обработчике.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

PREFIX = "ddp"


class DedupAction(StrEnum):
    SPLIT = "split"


@dataclass(slots=True, frozen=True)
class DedupCallback:
    action: DedupAction
    ticket_number: int


def encode_split(ticket_number: int) -> str:
    return f"{PREFIX}:{DedupAction.SPLIT.value}:{ticket_number}"


def decode(payload: str | None) -> DedupCallback | None:
    """None — не наша кнопка; никогда не бросает."""

    if not payload:
        return None
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    try:
        action = DedupAction(parts[1])
    except ValueError:
        return None
    if not parts[2].isdigit():
        return None
    return DedupCallback(action=action, ticket_number=int(parts[2]))
