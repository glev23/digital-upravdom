"""Разбор входящего payload MAX — единственное место, завязанное на точный
формат событий MAX Bot API.

**Контракт подтверждён MAX-001** (`documentation/max_api.md` §5, дата
сверки 22.09.2026, источник — `dev.max.ru/docs-api/objects/Update` и
`api-schema/schema.yaml`). Реальная форма `Update`:

- общие поля: `update_type`, `timestamp` (мс), `chat_id`;
- `message_created`: `message.sender.user_id`, `message.recipient.chat_id`,
  `message.body.mid` (уникальный ID сообщения — ключ идемпотентности),
  `message.body.text`;
- `bot_started`: `user.user_id`, `payload` (deep-link, ≤128 символов);
- `message_callback`: `callback.callback_id`, `callback.payload`,
  `callback.user.user_id`, чат — `message.recipient.chat_id` (уточнено по
  api-schema в ONBOARD-001: max_api.md §5 до этого описывал плоские
  `callback_id`/`button_payload`, которых в событии нет).

У MAX **нет** сквозного `update_id` — этим MAX отличается от
Telegram-подобных API, под которые была написана первая версия этого
файла. Для `message_created` идемпотентность строится на `message.body.mid`
(гарантированно уникален для MAX); для остальных типов — на составном
ключе `(update_type, chat_id, timestamp)`, который не гарантированно
уникален при действительно одновременных событиях (architecture.md §3) —
если это станет проблемой на практике, разбирать отдельно.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class ParsedEvent:
    event_id: str
    event_type: str
    """`update_type` как есть: `message_created`, `bot_started`,
    `message_callback` и т.д. (полный список — max_api.md §5)."""
    max_user_id: str
    chat_id: str
    text: str | None
    """Текст сообщения (`message_created`) или `callback.payload` (`message_callback`)."""
    start_payload: str | None = None
    """Deep-link параметр из `bot_started.payload` — используется ONBOARD-001."""
    callback_id: str | None = None
    """`callback.callback_id` — нужен для ответа на нажатие (`POST /answers`)."""


def _composite_id(update_type: str, chat_id: object, timestamp: object) -> str:
    """Ключ идемпотентности для событий без `message.body.mid` (раздел модуля)."""

    return f"{update_type}:{chat_id}:{timestamp}"


def parse_webhook_payload(payload: dict[str, Any]) -> ParsedEvent:
    update_type = str(payload.get("update_type") or "unknown")
    chat_id_raw = payload.get("chat_id", "")
    timestamp = payload.get("timestamp", "")

    if update_type == "message_created":
        message = payload.get("message") or {}
        sender = message.get("sender") or {}
        recipient = message.get("recipient") or {}
        body = message.get("body") or {}
        mid = body.get("mid")
        chat_id = str(recipient.get("chat_id", chat_id_raw))
        return ParsedEvent(
            event_id=str(mid) if mid else _composite_id(update_type, chat_id_raw, timestamp),
            event_type=update_type,
            max_user_id=str(sender.get("user_id", "")),
            chat_id=chat_id,
            text=body.get("text"),
        )

    if update_type == "bot_started":
        user = payload.get("user") or {}
        return ParsedEvent(
            event_id=_composite_id(update_type, chat_id_raw, timestamp),
            event_type=update_type,
            max_user_id=str(user.get("user_id", "")),
            chat_id=str(chat_id_raw),
            text=None,
            start_payload=payload.get("payload"),
        )

    if update_type == "message_callback":
        # api-schema `MessageCallbackUpdate` (сверено в ONBOARD-001): всё
        # вложено в `callback` — `payload`, `user`, `callback_id`, а
        # верхнеуровневого `chat_id` нет вовсе; чат берётся из исходного
        # сообщения с клавиатурой (`message` может быть null, если его удалили).
        callback = payload.get("callback") or {}
        user = callback.get("user") or {}
        message = payload.get("message") or {}
        recipient = message.get("recipient") or {}
        callback_id = callback.get("callback_id")
        # `callback_id` — идентификатор клавиатуры, а не нажатия: повторное
        # нажатие той же кнопки несёт тот же id, поэтому в ключ входит время.
        event_id = (
            f"{update_type}:{callback_id}:{callback.get('timestamp', timestamp)}"
            if callback_id
            else _composite_id(update_type, chat_id_raw, timestamp)
        )
        chat_id = recipient.get("chat_id") or chat_id_raw
        return ParsedEvent(
            event_id=event_id,
            event_type=update_type,
            max_user_id=str(user.get("user_id", "")),
            chat_id=str(chat_id) if chat_id else "",
            text=callback.get("payload"),
            callback_id=str(callback_id) if callback_id else None,
        )

    # Прочие подтверждённые типы update_type (bot_added, chat_title_changed
    # и т.п.) — Must Have их не обрабатывает; событие фиксируется для
    # идемпотентности и уходит в default-обработчик диспетчера как есть.
    return ParsedEvent(
        event_id=_composite_id(update_type, chat_id_raw, timestamp),
        event_type=update_type,
        max_user_id="",
        chat_id=str(chat_id_raw) if chat_id_raw else "",
        text=None,
    )
