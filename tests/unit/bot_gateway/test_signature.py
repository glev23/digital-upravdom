"""Проверка подлинности вебхука — критерий приёмки BOT-001.

Подтверждено MAX-001 (max_api.md §4): секрет передаётся обратно в
неизменном виде в заголовке `X-Max-Bot-Api-Secret`, это прямое сравнение
строк, не HMAC-подпись тела.
"""

from __future__ import annotations

from upravdom.bot_gateway.webhook import SIGNATURE_HEADER, verify_signature

SECRET = "test-secret"  # noqa: S105 — тестовый фикстурный секрет, не рабочий


def test_valid_secret_accepted() -> None:
    headers = {SIGNATURE_HEADER: SECRET}

    assert verify_signature(b"irrelevant body", headers, secret=SECRET) is True


def test_missing_header_rejected() -> None:
    assert verify_signature(b"irrelevant body", {}, secret=SECRET) is False


def test_wrong_secret_rejected() -> None:
    headers = {SIGNATURE_HEADER: "not-the-secret"}

    assert verify_signature(b"irrelevant body", headers, secret=SECRET) is False


def test_body_content_does_not_affect_verification() -> None:
    """MAX не подписывает тело — секрет верен независимо от содержимого body."""

    headers = {SIGNATURE_HEADER: SECRET}

    assert verify_signature(b'{"a": 1}', headers, secret=SECRET) is True
    assert verify_signature(b'{"a": 2}', headers, secret=SECRET) is True


def test_no_secret_configured_rejects_fail_closed() -> None:
    """Без настроенного секрета — отказ, а не пропуск проверки (fail-closed)."""

    headers = {SIGNATURE_HEADER: SECRET}

    assert verify_signature(b"irrelevant body", headers, secret=None) is False
