"""Строгий разбор кнопки `ddp:split:<номер>` (DEDUP-001)."""

from __future__ import annotations

import pytest

from upravdom.dedup.callbacks import DedupAction, decode, encode_split


def test_roundtrip() -> None:
    cb = decode(encode_split(1024))
    assert cb is not None
    assert cb.action is DedupAction.SPLIT
    assert cb.ticket_number == 1024


def test_payload_fits_max_limit() -> None:
    """Лимит payload кнопки — 1024 символа (max_api.md §7); кириллицы здесь нет."""

    payload = encode_split(9_999_999_999)
    assert len(payload) < 64
    assert payload.isascii()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "clf:st:1",
        "onb:accept:t:x:1",
        "ddp:split",
        "ddp:split:",
        "ddp:split:abc",
        "ddp:split:1:2",
        "ddp:merge:1",
        "ddp",
    ],
)
def test_foreign_or_broken_payload_is_none(payload: str | None) -> None:
    assert decode(payload) is None
