"""Fake-клиент MAX и чистая логика backoff — без сети и без БД."""

from __future__ import annotations

import pytest

from upravdom.bot_gateway.max_client import FakeMaxClient, MaxPermanentError, MaxTransientError
from upravdom.bot_gateway.outbox import _backoff_seconds


async def test_fake_client_records_sent_messages() -> None:
    client = FakeMaxClient()

    await client.send_message(chat_id="1", text="hello")

    assert client.sent == [("1", "hello")]


async def test_fake_client_transient_failure_then_success() -> None:
    client = FakeMaxClient(fail_times=1)

    with pytest.raises(MaxTransientError):
        await client.send_message(chat_id="1", text="hello")
    await client.send_message(chat_id="1", text="hello")  # второй вызов проходит

    assert client.sent == [("1", "hello")]


async def test_fake_client_permanent_failure_never_succeeds() -> None:
    client = FakeMaxClient(permanent_failure=True)

    with pytest.raises(MaxPermanentError):
        await client.send_message(chat_id="1", text="hello")


def test_backoff_grows_exponentially() -> None:
    assert _backoff_seconds(1, base_seconds=2.0) == 2.0
    assert _backoff_seconds(2, base_seconds=2.0) == 4.0
    assert _backoff_seconds(3, base_seconds=2.0) == 8.0


def test_backoff_never_negative_for_zero_attempts() -> None:
    assert _backoff_seconds(0, base_seconds=2.0) == 2.0
