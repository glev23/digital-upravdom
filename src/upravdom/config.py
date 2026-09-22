"""Конфигурация приложения из окружения.

Все параметры читаются из переменных окружения / `.env` через
`pydantic-settings`. Параметры хранилищ (``DATABASE_URL``, ``QDRANT_URL``)
обязательны — без них приложение не может проверить готовность (``/ready``)
и не стартует. Параметры внешних сервисов (MAX, OpenRouter) на этапе
INIT-001 необязательны: их код ещё не использует, и каркас должен подниматься
без токенов (architecture.md §1, §2).

См. `.env.example` в корне репозитория — единственный источник состава
переменных; при расхождении правится он, а не этот файл втихую.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Типизированные настройки приложения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Хранилища: обязательны, без них /ready не может ответить успехом ---
    database_url: str = Field(alias="DATABASE_URL")
    qdrant_url: str = Field(alias="QDRANT_URL")

    # --- MAX Bot API: пока необязательны (BOT-001, MAX-001) ------------------
    max_bot_token: str | None = Field(default=None, alias="MAX_BOT_TOKEN")
    max_webhook_secret: str | None = Field(default=None, alias="MAX_WEBHOOK_SECRET")
    max_bot_username: str | None = Field(default=None, alias="MAX_BOT_USERNAME")
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")
    # Подтверждено MAX-001 (max_api.md §2): platform-api2.max.ru — актуальный
    # хост (не platform-api.max.ru без цифры — это старый адрес).
    max_api_base_url: str | None = Field(
        default="https://platform-api2.max.ru", alias="MAX_API_BASE_URL"
    )

    # --- LLM (OpenRouter): пока необязательны (LLM-001) -----------------------
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_model: str | None = Field(default=None, alias="OPENROUTER_MODEL")
    openrouter_model_fallback: str | None = Field(default=None, alias="OPENROUTER_MODEL_FALLBACK")
    llm_timeout_seconds: float = Field(default=8.0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_requests_per_minute: int = Field(default=15, alias="LLM_MAX_REQUESTS_PER_MINUTE")
    llm_circuit_failure_threshold: int = Field(default=3, alias="LLM_CIRCUIT_FAILURE_THRESHOLD")
    llm_circuit_open_seconds: float = Field(default=60.0, alias="LLM_CIRCUIT_OPEN_SECONDS")

    # --- Эмбеддинги (SPIKE-001, KB-001) ---------------------------------------
    embedding_model_name: str = Field(default="sergeyzh/BERTA", alias="EMBEDDING_MODEL_NAME")

    # --- Пороги классификации (architecture.md §4) ----------------------------
    classify_confidence_high: float = Field(default=0.75, alias="CLASSIFY_CONFIDENCE_HIGH")
    classify_confidence_low: float = Field(default=0.45, alias="CLASSIFY_CONFIDENCE_LOW")
    semantic_cache_threshold: float = Field(default=0.92, alias="SEMANTIC_CACHE_THRESHOLD")
    # Часовой пояс дома для срока в ответах жителю (FLOW-001).
    display_timezone: str = Field(default="Europe/Moscow", alias="DISPLAY_TIMEZONE")

    # --- Персональные данные (architecture.md §11.1) --------------------------
    consent_version: int = Field(default=1, alias="CONSENT_VERSION")
    privacy_policy_url: str | None = Field(default=None, alias="PRIVACY_POLICY_URL")
    # Общий контакт, если дом не подключён (ONBOARD-002).
    fallback_contact_text: str = Field(
        default=(
            "При аварии, опасной для жизни, звоните 112. По остальным вопросам "
            "обратитесь в свою управляющую компанию — контакты обычно есть на квитанции."
        ),
        alias="FALLBACK_CONTACT_TEXT",
    )

    # --- Ограничение частоты (architecture.md §10) -----------------------------
    rate_limit_messages_per_minute: int = Field(default=10, alias="RATE_LIMIT_MESSAGES_PER_MINUTE")

    # --- Воркер inbox/outbox (BOT-001, architecture.md §3, §7) -----------------
    worker_poll_interval_seconds: float = Field(default=1.0, alias="WORKER_POLL_INTERVAL_SECONDS")
    worker_batch_size: int = Field(default=10, alias="WORKER_BATCH_SIZE")
    inbound_visibility_timeout_seconds: int = Field(
        default=120, alias="INBOUND_VISIBILITY_TIMEOUT_SECONDS"
    )
    inbound_max_attempts: int = Field(default=5, alias="INBOUND_MAX_ATTEMPTS")
    outbound_max_attempts: int = Field(default=5, alias="OUTBOUND_MAX_ATTEMPTS")
    outbound_backoff_base_seconds: float = Field(default=2.0, alias="OUTBOUND_BACKOFF_BASE_SECONDS")


@lru_cache
def get_settings() -> Settings:
    """Настройки читаются один раз за процесс и кэшируются."""

    return Settings()  # type: ignore[call-arg]  # значения приходят из окружения
