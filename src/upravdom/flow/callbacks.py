"""Формат callback-кнопок основного сценария (FLOW-001).

`clf:ans:<log_id>:<idx>` / `clf:none:<log_id>` / `clf:st` / `clf:st:<number>`
Текст вопроса в payload не кладётся — лимит кириллицы (урок ONBOARD-002).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

PREFIX = "clf"
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class FlowAction(StrEnum):
    ANSWER = "ans"
    NONE = "none"
    STATUS = "st"


@dataclass(slots=True, frozen=True)
class FlowCallback:
    action: FlowAction
    log_id: uuid.UUID | None = None
    option_index: int | None = None
    ticket_number: int | None = None


def encode_answer(log_id: uuid.UUID, option_index: int) -> str:
    return f"{PREFIX}:{FlowAction.ANSWER.value}:{log_id}:{option_index}"


def encode_none(log_id: uuid.UUID) -> str:
    return f"{PREFIX}:{FlowAction.NONE.value}:{log_id}"


def encode_status(ticket_number: int | None = None) -> str:
    if ticket_number is None:
        return f"{PREFIX}:{FlowAction.STATUS.value}"
    return f"{PREFIX}:{FlowAction.STATUS.value}:{ticket_number}"


def decode(payload: str | None) -> FlowCallback | None:
    """None — не наша кнопка; никогда не бросает."""

    if not payload:
        return None
    parts = payload.split(":")
    if not parts or parts[0] != PREFIX:
        return None
    try:
        action = FlowAction(parts[1])
    except (ValueError, IndexError):
        return None

    if action is FlowAction.STATUS:
        if len(parts) == 2:
            return FlowCallback(action=action)
        if len(parts) == 3 and parts[2].isdigit():
            return FlowCallback(action=action, ticket_number=int(parts[2]))
        return None

    if len(parts) < 3 or not _UUID_RE.match(parts[2]):
        return None
    log_id = uuid.UUID(parts[2])
    if action is FlowAction.NONE:
        if len(parts) != 3:
            return None
        return FlowCallback(action=action, log_id=log_id)
    if action is FlowAction.ANSWER:
        if len(parts) != 4 or not parts[3].isdigit():
            return None
        return FlowCallback(action=action, log_id=log_id, option_index=int(parts[3]))
    return None
