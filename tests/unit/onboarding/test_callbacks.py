"""Формат payload кнопок онбординга: обратимость и отказ на мусор."""

from __future__ import annotations

import pytest

from upravdom.onboarding.callbacks import (
    Action,
    OnboardingCallback,
    RefKind,
    decode,
    encode,
    inline_keyboard,
)


@pytest.mark.parametrize(
    "cb",
    [
        OnboardingCallback(Action.ACCEPT, RefKind.TOKEN, "9d7984baecef5909", 1),
        OnboardingCallback(Action.DECLINE, RefKind.TOKEN, "9d7984baecef5909", 3),
        OnboardingCallback(Action.ACCEPT, RefKind.HOUSE, "0b5c7a52-7b1f-5d0e-9a3e-2f5e1c9d4a11", 1),
        OnboardingCallback(
            Action.ACCEPT, RefKind.SEARCH, "0b5c7a52-7b1f-5d0e-9a3e-2f5e1c9d4a11", 1
        ),
        OnboardingCallback(Action.WRONG_HOUSE, RefKind.TOKEN, "9d7984baecef5909"),
        OnboardingCallback(Action.ADD_HOUSE, RefKind.TOKEN, "9d7984baecef5909"),
        OnboardingCallback(Action.PICK, RefKind.SEARCH, "0b5c7a52-7b1f-5d0e-9a3e-2f5e1c9d4a11"),
        OnboardingCallback(Action.RETRY, RefKind.NONE, "-"),
        OnboardingCallback(Action.NONE_MATCH, RefKind.ADDR, "RGVrYWJyaXN0b3YgMTA"),
        OnboardingCallback(Action.LEAVE, RefKind.ADDR, "RGVrYWJyaXN0b3YgMTA"),
    ],
)
def test_roundtrip(cb: OnboardingCallback) -> None:
    payload = encode(cb)

    assert decode(payload) == cb
    assert len(payload) <= 1024  # лимит payload callback-кнопки, max_api.md §7


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "confirm",
        "onb",
        "onb:accept",
        "onb:accept:t:tok",  # согласие без версии
        "onb:accept:t:tok:v1",  # версия не число
        "onb:accept:t:tok:1:extra",
        "onb:wrong:t:tok:1",  # у «не мой дом» версии нет
        "onb:explode:t:tok",
        "onb:accept:x:tok:1",
        "onb:accept:t:bad token:1",
        "onb:accept:t::1",
        "xyz:accept:t:tok:1",
    ],
)
def test_rejects_garbage(payload: str | None) -> None:
    assert decode(payload) is None


def test_inline_keyboard_shape() -> None:
    attachments = inline_keyboard([[("Да", "p1"), ("Нет", "p2")], [("Иное", "p3")]])

    assert attachments == [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {"type": "callback", "text": "Да", "payload": "p1"},
                        {"type": "callback", "text": "Нет", "payload": "p2"},
                    ],
                    [{"type": "callback", "text": "Иное", "payload": "p3"}],
                ]
            },
        }
    ]
