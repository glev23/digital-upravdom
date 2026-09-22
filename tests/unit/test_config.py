"""Тесты конфигурации: обязательные параметры хранилищ и безопасность ошибки.

Критерий приёмки INIT-001: отсутствие обязательного параметра даёт понятную
ошибку старта без печати значений остальных переменных.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from upravdom.config import Settings


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:55432/db")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:56333")

    # `_env_file=None`: тест не должен зависеть от того, что реально лежит
    # в `.env` разработчика (токены, реальные URL) — только от monkeypatch.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.database_url == "postgresql+asyncpg://u:p@localhost:55432/db"
    assert settings.qdrant_url == "http://localhost:56333"
    # необязательные параметры внешних сервисов не заданы — это ожидаемо на INIT-001
    assert settings.max_bot_token is None
    assert settings.openrouter_api_key is None
    # значения по умолчанию из architecture.md §4, §6.2, §10, §11.1
    assert settings.classify_confidence_high == 0.75
    assert settings.classify_confidence_low == 0.45
    assert settings.semantic_cache_threshold == 0.92
    assert settings.consent_version == 1
    assert settings.rate_limit_messages_per_minute == 10


def test_settings_require_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:56333")

    # `_env_file=None` отключает чтение `.env` из репозитория: без него тест
    # находил бы реальный DATABASE_URL разработчика в корневом `.env` и не
    # проверял бы обязательность параметра.
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert "database_url" in str(exc_info.value).lower()


def test_settings_require_qdrant_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:55432/db")
    monkeypatch.delenv("QDRANT_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert "qdrant_url" in str(exc_info.value).lower()
