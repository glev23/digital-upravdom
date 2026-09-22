"""Разбор payload по подтверждённой MAX-001 схеме `Update` (max_api.md §5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from upravdom.bot_gateway.schemas import parse_webhook_payload


def test_parses_message_created() -> None:
    payload = {
        "update_type": "message_created",
        "timestamp": 1700000000000,
        "chat_id": 777,
        "message": {
            "sender": {"user_id": 42, "first_name": "Житель"},
            "recipient": {"chat_id": 777, "chat_type": "dialog"},
            "body": {"mid": "mid-abc123", "seq": 5, "text": "нет воды"},
        },
    }

    parsed = parse_webhook_payload(payload)

    assert parsed.event_id == "mid-abc123"  # ключ идемпотентности — mid, не update_id
    assert parsed.event_type == "message_created"
    assert parsed.max_user_id == "42"
    assert parsed.chat_id == "777"
    assert parsed.text == "нет воды"
    assert parsed.start_payload is None


def test_parses_bot_started_with_deep_link_payload() -> None:
    payload = {
        "update_type": "bot_started",
        "timestamp": 1700000000001,
        "chat_id": 777,
        "user": {"user_id": 42},
        "payload": "house-token-abc",
    }

    parsed = parse_webhook_payload(payload)

    assert parsed.event_type == "bot_started"
    assert parsed.max_user_id == "42"
    assert parsed.start_payload == "house-token-abc"
    assert parsed.text is None


def _callback_fixture() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "fixtures" / "max" / "message_callback.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def test_parses_message_callback() -> None:
    """Живой образец с VM (ONBOARD-001) — nested callback.*, не плоские поля docs."""

    fixture = _callback_fixture()
    parsed = parse_webhook_payload(fixture)

    assert parsed.event_type == "message_callback"
    assert parsed.max_user_id == str(fixture["callback"]["user"]["user_id"])
    assert parsed.chat_id == str(fixture["message"]["recipient"]["chat_id"])
    assert parsed.text == fixture["callback"]["payload"]
    assert parsed.callback_id == fixture["callback"]["callback_id"]
    assert parsed.text is not None
    assert parsed.text.startswith("onb:accept:")


def test_repeated_press_of_same_keyboard_is_a_new_event() -> None:
    first = _callback_fixture()
    second = _callback_fixture()
    second["callback"]["timestamp"] += 5000

    assert parse_webhook_payload(first).event_id != parse_webhook_payload(second).event_id


def test_callback_with_deleted_message_has_no_chat() -> None:
    payload = _callback_fixture()
    payload["message"] = None

    assert parse_webhook_payload(payload).chat_id == ""


def test_unrecognized_update_type_has_stable_composite_id() -> None:
    payload = {"update_type": "chat_title_changed", "timestamp": 1700000000003, "chat_id": 777}

    first = parse_webhook_payload(payload)
    second = parse_webhook_payload(payload)

    assert first.event_id == second.event_id
    assert first.event_id == "chat_title_changed:777:1700000000003"
    assert first.max_user_id == ""


def test_message_created_without_mid_falls_back_to_composite_id() -> None:
    """У message_created в норме всегда есть mid — но код не должен падать без него."""

    payload = {
        "update_type": "message_created",
        "timestamp": 1700000000004,
        "chat_id": 777,
        "message": {
            "sender": {"user_id": 42},
            "recipient": {"chat_id": 777},
            "body": {"text": "x"},
        },
    }

    parsed = parse_webhook_payload(payload)

    assert parsed.event_id == "message_created:777:1700000000004"


def test_missing_update_type_is_unknown() -> None:
    parsed = parse_webhook_payload({"something": "else"})

    assert parsed.event_type == "unknown"
    assert parsed.max_user_id == ""
