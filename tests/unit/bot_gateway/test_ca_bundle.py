"""Выбор CA-бандла для HttpxMaxClient (max_api.md §3 — Russian Trusted CA)."""

from __future__ import annotations

from pathlib import Path

import pytest

from upravdom.bot_gateway.max_client import SYSTEM_CA_BUNDLE, HttpxMaxClient, _default_ca_bundle


def test_uses_system_bundle_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "exists", lambda self: True)

    assert _default_ca_bundle() == SYSTEM_CA_BUNDLE


def test_falls_back_to_default_verification_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert _default_ca_bundle() is True


def test_explicit_ca_bundle_overrides_default() -> None:
    client = HttpxMaxClient(base_url="https://example.com", token="t", ca_bundle=True)  # noqa: S106

    assert client._client is not None  # клиент создался без исключения
