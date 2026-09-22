"""Формат `payload` кнопок онбординга (ONBOARD-001 / ONBOARD-002).

Состояние «какой дом / какой адрес» живёт в кнопке, а не в таблице сессий.
Лимит payload callback-кнопки MAX — 1024 символа (max_api.md §7).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from enum import StrEnum

PREFIX = "onb"
# UUID, токены и urlsafe-base64 адреса (без паддинга `=`).
_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class Action(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    WRONG_HOUSE = "wrong"
    ADD_HOUSE = "add"
    PICK = "pick"
    """Выбор дома из результатов поиска адреса."""
    NONE_MATCH = "none"
    """«Моего дома нет в списке» / переход в ветку «не найден»."""
    RETRY = "retry"
    """«Ввести адрес ещё раз»."""
    LEAVE = "leave"
    """«Оставить адрес» → house_requests."""


class RefKind(StrEnum):
    TOKEN = "t"
    """Токен `house_links` — дом из deep-link."""
    HOUSE = "h"
    """`houses.id` уже привязанного дома — повторное согласие."""
    SEARCH = "s"
    """`houses.id` из поиска по адресу → `consents.source = address_search`."""
    ADDR = "a"
    """urlsafe-base64 текста адреса (ветка «не найден» / оставить адрес)."""
    NONE = "x"
    """Нет полезной ссылки (retry без сохранённого адреса)."""


_VERSIONED = {Action.ACCEPT, Action.DECLINE}
_CONSENT_KINDS = {RefKind.TOKEN, RefKind.HOUSE, RefKind.SEARCH}
_LINK_ACTIONS = {Action.WRONG_HOUSE, Action.ADD_HOUSE}
_ADDR_ACTIONS = {Action.NONE_MATCH, Action.LEAVE}


@dataclass(slots=True, frozen=True)
class OnboardingCallback:
    action: Action
    ref_kind: RefKind
    ref_value: str
    consent_version: int | None = None


def encode_address(text: str) -> str:
    """Адрес в payload кнопки: urlsafe base64 без `=`."""

    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def decode_address(value: str) -> str | None:
    try:
        pad = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def encode(cb: OnboardingCallback) -> str:
    parts = [PREFIX, cb.action.value, cb.ref_kind.value, cb.ref_value]
    if cb.action in _VERSIONED:
        parts.append(str(cb.consent_version))
    return ":".join(parts)


def _kind_ok(action: Action, ref_kind: RefKind) -> bool:
    if action in _VERSIONED:
        return ref_kind in _CONSENT_KINDS
    if action in _LINK_ACTIONS:
        return ref_kind is RefKind.TOKEN
    if action is Action.PICK:
        return ref_kind is RefKind.SEARCH
    if action in _ADDR_ACTIONS:
        return ref_kind is RefKind.ADDR
    if action is Action.RETRY:
        return ref_kind in {RefKind.NONE, RefKind.ADDR}
    return False


def decode(payload: str | None) -> OnboardingCallback | None:
    """None — не наша кнопка или испорченный payload; никогда не бросает."""

    if not payload:
        return None
    parts = payload.split(":")
    if len(parts) < 4 or parts[0] != PREFIX:
        return None
    try:
        action = Action(parts[1])
        ref_kind = RefKind(parts[2])
    except ValueError:
        return None
    ref_value = parts[3]
    if not _VALUE_RE.match(ref_value):
        return None
    if not _kind_ok(action, ref_kind):
        return None

    if action in _VERSIONED:
        if len(parts) != 5 or not parts[4].isdigit():
            return None
        return OnboardingCallback(action, ref_kind, ref_value, int(parts[4]))
    if len(parts) != 4:
        return None
    return OnboardingCallback(action, ref_kind, ref_value)


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> list[dict[str, object]]:
    """Вложение `inline_keyboard` из строк `(текст, payload)` (max_api.md §7)."""

    buttons = [
        [{"type": "callback", "text": text, "payload": payload} for text, payload in row]
        for row in rows
    ]
    return [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
