"""Клиент MAX Bot API — порт + реализации (architecture.md §7).

Обработчики никогда не вызывают этот клиент напрямую — только через
`outbox.py`, который ставит сообщение в очередь `outbound_messages`
(architecture.md §7: «прямая отправка из обработчика запрещена»).

**Контракт подтверждён MAX-001** (`documentation/max_api.md` §1, §2, §7,
дата сверки 22.09.2026): `POST /messages` на `https://platform-api2.max.ru`,
адресат — query-параметр `chat_id` **или** `user_id`, авторизация —
заголовок `Authorization: <token>` **без схемы `Bearer`** (частая ошибка
при переносе кода с других мессенджер-API — сюда же чуть не попала).
Коды постоянного отказа (403/410 — "бот заблокирован" и т.п.) остаются
предположением: MAX-001 не нашла для них отдельной документации, только
общее HTTP-соглашение "4xx — ошибка клиента".

**TLS: сертификат `platform-api2.max.ru` подписан Russian Trusted CA**
(Минцифры) — подтверждено живым запросом 22.09.2026. `httpx` по умолчанию
проверяет TLS через бандл `certifi`, а не через системное хранилище ОС;
`certifi` этот корневой центр не включает — без явного `verify=` ниже
любой вызов упал бы с `unable to get local issuer certificate`, несмотря
на то что сертификаты Минцифры уже добавлены в доверенные ОС
(`docker/app.Dockerfile`, `update-ca-certificates`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx

# Собранный `update-ca-certificates` бандл: стандартный набор Mozilla/Debian
# + Russian Trusted Root/Sub CA (см. docker/app.Dockerfile). Путь — то же
# место, куда update-ca-certificates кладёт результат на Debian/Ubuntu
# (python:3.12-slim) — специфичен для контейнера, не существует локально
# на Windows/macOS вне Docker.
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def _default_ca_bundle() -> str | bool:
    """В Docker — собранный бандл с Russian Trusted CA. Вне Docker (venv
    на Windows/macOS) такого пути нет — используется обычная проверка
    httpx (`certifi`, без Russian CA). Вызовы к `platform-api2.max.ru`
    тогда завершатся TLS-ошибкой, но `send_message` превращает её в
    `MaxTransientError` — воркер не падает, просто не может отправить,
    что и так ожидаемо для локальной разработки без нужного CA."""

    return SYSTEM_CA_BUNDLE if Path(SYSTEM_CA_BUNDLE).exists() else True


class MaxClientError(Exception):
    """Базовая ошибка отправки — не создаётся напрямую."""


class MaxTransientError(MaxClientError):
    """Временный сбой (таймаут, 5xx, недоступность сети) — стоит повторить."""


class MaxPermanentError(MaxClientError):
    """Постоянный сбой (например, пользователь заблокировал бота) — повтор бессмыслен."""


class MaxClient(Protocol):
    """Порт: всё, что нужно `outbox.py` от клиента MAX."""

    async def send_message(
        self, *, chat_id: str, text: str, attachments: list[dict[str, object]] | None = None
    ) -> None: ...

    async def answer_callback(self, *, callback_id: str, notification: str) -> None: ...


class HttpxMaxClient:
    """Реализация на `httpx` по подтверждённому контракту (`POST /messages`)."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 10.0,
        ca_bundle: str | bool | None = None,
    ) -> None:
        self._token = token
        resolved_ca_bundle = _default_ca_bundle() if ca_bundle is None else ca_bundle
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_seconds, verify=resolved_ca_bundle
        )

    async def send_message(
        self, *, chat_id: str, text: str, attachments: list[dict[str, object]] | None = None
    ) -> None:
        body: dict[str, object] = {"text": text}
        if attachments:
            body["attachments"] = attachments
        await self._post("/messages", params={"chat_id": chat_id}, body=body)

    async def answer_callback(self, *, callback_id: str, notification: str) -> None:
        """`POST /answers` (api-schema `answerOnCallback`, сверено 22.09.2026 в
        ONBOARD-001): снимает ожидание с нажатой кнопки у жителя."""

        await self._post(
            "/answers", params={"callback_id": callback_id}, body={"notification": notification}
        )

    async def _post(self, path: str, *, params: dict[str, str], body: dict[str, object]) -> None:
        try:
            response = await self._client.post(
                path, params=params, json=body, headers={"Authorization": self._token}
            )
        except httpx.TimeoutException as exc:
            raise MaxTransientError("timeout") from exc
        except httpx.TransportError as exc:
            raise MaxTransientError("transport error") from exc

        if response.status_code in (403, 410):
            # Не подтверждено MAX-001 отдельно: типичные коды "бот
            # заблокирован" / "чат не существует" у мессенджер-платформ.
            raise MaxPermanentError(f"permanent failure: HTTP {response.status_code}")
        if response.status_code >= 500:
            raise MaxTransientError(f"server error: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise MaxTransientError(f"client error: HTTP {response.status_code}")

    async def aclose(self) -> None:
        await self._client.aclose()


class FakeMaxClient:
    """Для тестов: пишет в память, без сети, управляемо падает."""

    def __init__(self, *, fail_times: int = 0, permanent_failure: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.sent_attachments: list[list[dict[str, object]] | None] = []
        self.answers: list[tuple[str, str]] = []
        self._fail_times = fail_times
        self._permanent_failure = permanent_failure
        self.calls = 0

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self._permanent_failure:
            raise MaxPermanentError("fake permanent failure")
        if self.calls <= self._fail_times:
            raise MaxTransientError("fake transient failure")

    async def send_message(
        self, *, chat_id: str, text: str, attachments: list[dict[str, object]] | None = None
    ) -> None:
        self._maybe_fail()
        self.sent.append((chat_id, text))
        self.sent_attachments.append(attachments)

    async def answer_callback(self, *, callback_id: str, notification: str) -> None:
        self._maybe_fail()
        self.answers.append((callback_id, notification))
