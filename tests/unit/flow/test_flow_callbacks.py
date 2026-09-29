"""Разбор clf:* callback payload (FLOW-001)."""

from __future__ import annotations

import uuid

from upravdom.flow.callbacks import (
    FlowAction,
    decode,
    encode_answer,
    encode_none,
    encode_norm,
    encode_status,
)


def test_roundtrip_answer() -> None:
    log_id = uuid.uuid4()
    payload = encode_answer(log_id, 2)
    cb = decode(payload)
    assert cb is not None
    assert cb.action is FlowAction.ANSWER
    assert cb.log_id == log_id
    assert cb.option_index == 2


def test_roundtrip_none_and_status() -> None:
    log_id = uuid.uuid4()
    none_cb = decode(encode_none(log_id))
    assert none_cb is not None
    assert none_cb.action is FlowAction.NONE
    st = decode(encode_status())
    assert st is not None and st.ticket_number is None
    st2 = decode(encode_status(1024))
    assert st2 is not None and st2.ticket_number == 1024


def test_roundtrip_norm() -> None:
    chunk_id = uuid.uuid4()
    payload = encode_norm(chunk_id)
    # Лимит payload мал — в нём только id, не текст пункта.
    assert len(payload) < 64
    cb = decode(payload)
    assert cb is not None
    assert cb.action is FlowAction.NORM
    assert cb.chunk_id == chunk_id


def test_rejects_garbage() -> None:
    assert decode(None) is None
    assert decode("onb:accept:t:x:1") is None
    assert decode("clf:ans:not-a-uuid:0") is None
    assert decode("clf:ans") is None
    assert decode("clf:norm") is None
    assert decode("clf:norm:not-a-uuid") is None
    assert decode(f"clf:norm:{uuid.uuid4()}:1") is None
