# MAX Bot API — подтверждённый контракт (MAX-001)

**Дата сверки:** 22.09.2026. **Источник:** официальная документация
[dev.max.ru/docs-api](https://dev.max.ru/docs-api) и спецификация
[github.com/max-messenger/api-schema](https://github.com/max-messenger/api-schema)
(`schema.yaml`, ветка `main`). Документация MAX развивается — перед сдачей
сверить ссылки заново (дата изменения могла сдвинуться).

Статус каждого факта: **подтверждено документацией** / **подтверждено живым
запросом** / **не подтверждено**. Факты из статей третьих лиц, библиотек и
форумов ниже не используются как источник — только официальные страницы.

---

## 1. Авторизация

**Подтверждено документацией.** Токен передаётся в заголовке
`Authorization: <access_token>` — **без схемы `Bearer`**, сырым значением.
Передача через query-параметры отменена.

```
Authorization: {access_token}
```

Источник: [dev.max.ru/docs-api/methods/POST/messages](https://dev.max.ru/docs-api/methods/POST/messages).

**Расхождение с BOT-001:** `HttpxMaxClient` использовал
`Authorization: Bearer {token}` — неверно, требует правки (см. раздел 8).

## 2. Базовый адрес API

**Подтверждено документацией.** `https://platform-api2.max.ru` (актуальный
хост; в части старых материалов встречается `platform-api.max.ru` без
цифры — не использовать, это прежний адрес).

Источник: спецификация `schema.yaml` (`servers`), подтверждено примером
curl на странице `POST /messages`.

**Расхождение:** `MAX_API_BASE_URL` в `.env.example` был оставлен пустым
до подтверждения — теперь можно заполнить дефолтом (раздел 8).

## 3. Получение событий: вебхук

**Подтверждено документацией.** `POST /subscriptions` регистрирует
webhook:

| Поле | Требование |
|---|---|
| `url` | обязателен, HTTPS, порт только 443 |
| `update_types` | опционально — список типов событий, см. раздел 5 |
| `secret` | опционально, 5–256 символов, латиница/цифры/дефис |

**TLS:** только сертификаты доверенных ЦС или Минцифры РФ; self-signed не
поддерживаются. Сервер обязан отдавать полную цепочку сертификатов.

**Подтверждено живым запросом 22.09.2026 (важно для клиентской стороны,
не только для вебхука):** сам `platform-api2.max.ru` тоже использует
сертификат, подписанный Russian Trusted CA (Минцифры), а не публичным
Западным ЦС. `httpx` (и вообще любой клиент, использующий `certifi`, а не
системное хранилище ОС) по умолчанию не доверяет этому корневому центру
и падает с `unable to get local issuer certificate` при любом исходящем
запросе к MAX — раньше это было незамечено, потому что живых вызовов не
делали. Официальные сертификаты — `russian_trusted_root_ca_pem.crt` и
`russian_trusted_sub_ca.cer` с [gu-st.ru](https://gu-st.ru) (ресурс
Минцифры). Решение и код — `docker/app.Dockerfile`
(`update-ca-certificates`) + `bot_gateway/max_client.py`
(`verify=SYSTEM_CA_BUNDLE`).

**Таймаут ответа:** обработчик обязан ответить `200` в течение **30 секунд**
(architecture.md уже отвечает до обработки — запас большой).

**Повторы при сбое:** экспоненциальный backoff, начальная задержка **60 с**,
множитель **×2.5**, до **10 попыток**; автоотписка через **8 часов** без
успешного ответа.

Источник: [dev.max.ru/docs-api/methods/POST/subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions).

**Расхождение с architecture.md §10:** таблица отказов говорит «MAX
повторит доставку» без цифр — теперь можно указать точные параметры backoff.

## 4. Подлинность вебхука

**Подтверждено документацией — и это меняет реализацию BOT-001.**

> «Параметр `secret` позволяет убедиться, что Webhook-запросы приходят от
> MAX, а не от третьей стороны. Если `secret` указан при создании
> подписки, он передаётся в заголовке `X-Max-Bot-Api-Secret`.»

Секрет передаётся **в неизменном виде**, не как HMAC-подпись тела запроса.
Проверка — сравнение значения заголовка с настроенным секретом (константное
время сравнения, не через `==`).

| Было в BOT-001 (best-effort) | Реально |
|---|---|
| Заголовок `X-Max-Signature` | Заголовок `X-Max-Bot-Api-Secret` |
| HMAC-SHA256 от тела запроса | Секрет передаётся как есть, сравнение строк |

**Требует правки** `src/upravdom/bot_gateway/webhook.py` — раздел 8.

## 5. Формат событий (`Update`)

**Подтверждено документацией** (объект `Update`, схема `api-schema`).

**Общие поля любого события:** `update_type` (string), `timestamp` (int64,
мс), `chat_id` (int64).

**`message_created`** (свободный текст жителя — основной случай):
- `message.sender` — объект `User` (`user_id`, `first_name`, `last_name?`,
  `username?`, `is_bot`)
- `message.recipient` — `chat_id`, `chat_type`, `user_id`
- `message.body.mid` (string) — **уникальный ID сообщения, естественный
  ключ идемпотентности** вместо `update_id`, которого в MAX нет вообще
- `message.body.seq` (int64) — порядковый номер в чате
- `message.body.text` (string, nullable)

**`bot_started`** (для онбординга / deep-link):
- `user` — объект `User`
- `payload` (string, ≤128 символов) — значение `start`-параметра

**`message_callback`** (нажатие inline-кнопки):

**Подтверждено живым запросом** (ONBOARD-001, 22.09.2026, VM
`digital-upravdom.duckdns.org`). Реальная форма события (не плоские
`callback_id` / `button_payload` из краткого описания docs-api):

- `callback.callback_id` — id нажатия (для `POST /answers`);
- `callback.payload` — строка кнопки (у нас `onb:accept:t:<token>:<ver>`);
- `callback.user.user_id` — кто нажал;
- `message.recipient.chat_id` — чат для ответа;
- `message.body` — исходное сообщение с клавиатурой (текст + attachments);
- `message.sender` — бот (если сообщение с кнопками от бота).

Образец: `tests/fixtures/max/message_callback.json`.

`POST /answers` (`callback_id` + `notification`) — **подтверждён живьём**:
после «Согласен» часики на кнопке снимаются, в outbox уходит
`{"callback_id": "...", "notification": "Принято"}`.

Полный список типов `update_type` (для `update_types` при подписке и для
диспетчера): `bot_added`, `bot_removed`, `bot_started`, `bot_stopped`,
`message_created`, `message_edited`, `message_removed`, `message_callback`,
`comment_created`, `comment_edited`, `comment_removed`, `user_added`,
`user_removed`, `chat_title_changed`, `dialog_cleared`, `dialog_muted`,
`dialog_unmuted`, `dialog_removed`, `bot_admin_permissions_changed`.

Источник: [dev.max.ru/docs-api/objects/Update](https://dev.max.ru/docs-api/objects/Update),
`schema.yaml`; живой образец — ONBOARD-001.

**Расхождение с BOT-001:** `schemas.py` был написан по Telegram-подобной
догадке (`update_id`, `message.from.id`, `message.chat.id`) — структура
похожая, но поля называются иначе, и **у MAX нет `update_id`** — для
`message_created` идемпотентность строится на `message.body.mid`, а не на
синтетическом хэше payload. Требует правки — раздел 8.

**Не подтверждено:** для `bot_started` естественного уникального
идентификатора события нет (только `user_id` + `timestamp` + `payload`).
Если MAX продублирует доставку `bot_started`, дедуп по нему слабее, чем по
`message.body.mid`. Комбинация `(chat_id, timestamp, update_type)` — рабочий
компромисс, но не гарантированно уникальна при одновременных событиях;
если это окажется проблемой на практике — завести отдельный тикет.

## 6. Deep-link со стартовым параметром

**Подтверждено документацией.**

- Формат: `https://max.ru/<ник_бота>?start=<payload>`.
- Ограничение: **до 128 символов**, без спецсимволов, требующих
  URL-кодирования (пробелы, кириллица) — только допустимые для URL символы.
- Значение приходит в поле `payload` события `bot_started` (раздел 5).
- Не путать с deep-link мини-приложений — там параметр `startapp`, другой
  путь.

Источник: комментарий в спецификации `schema.yaml` + подтверждающие
источники: [issue #299 max-bot-api-client-ts](https://github.com/max-messenger/max-bot-api-client-ts/issues/299).

**Для платформенного бонуса (idea_and_scope.md, шаг 7): подтверждено —
стартовый параметр deep-link поддерживается.** `house_links.token`
(architecture.md §5.1) укладывается в 128 символов без проблем (UUID/base62
токен короче). Требование «без кириллицы и спецсимволов» уже совместимо с
непрозрачным токеном.

## 7. Отправка сообщений и клавиатура

**Подтверждено документацией.**

`POST /messages` — адресат через **query-параметры** `chat_id` **или**
`user_id` (обязателен один из двух), плюс `disable_link_preview` (bool).
Тело запроса (`NewMessageBody`):

```json
{
  "text": "строка, до 4000 символов",
  "format": "markdown | html (опционально)",
  "notify": true,
  "attachments": [],
  "link": null
}
```

Обязательно наличие `text` **или** `attachments`.

**Inline-клавиатура** — вложение типа `inline_keyboard`:

```json
{
  "type": "inline_keyboard",
  "payload": {"buttons": [[{"type": "callback", "text": "…", "payload": "…"}]]}
}
```

Типы кнопок: `callback` (payload ≤1024), `link` (url ≤2048), `message`,
`request_contact`, `request_geo_location` (+ `quick`), `open_app` (для
мини-приложений, `web_app` + `payload` ≤512), `clipboard` (payload ≤1024).

Источник: [dev.max.ru/docs-api/methods/POST/messages](https://dev.max.ru/docs-api/methods/POST/messages),
[dev.max.ru/docs-api/objects/NewMessageBody](https://dev.max.ru/docs-api/objects/NewMessageBody), `schema.yaml`.

**Расхождение с BOT-001:** `HttpxMaxClient.send_message` уже случайно верно
угадал `chat_id` как query-параметр и путь `/messages` — но не `user_id`
как альтернативу, и не поддерживал `format`. Не критично для Must Have
(простой текст без разметки), можно доработать при необходимости в
CLASSIFY-001/ONBOARD-001, когда понадобится markdown в ответах.

## 8. Long polling

**Подтверждено документацией.** `GET /updates` — доступен как альтернатива
вебхуку, **не требует публичного HTTPS**.

| Параметр | Значение |
|---|---|
| `timeout` | 0–90 с, по умолчанию 30 |
| `limit` | 1–1000, по умолчанию 100 |
| `marker` | int64, nullable — курсор на следующую страницу |

Явно помечено как **не предназначенное для продакшна** («ограничено по
скорости и сроку хранения событий... Webhook обязателен для
production»), но полностью пригодно для разработки и живой проверки без
DEPLOY-001.

**Открывает возможность:** BOT-001 и ONBOARD-001 можно проверить живьём
через `GET /updates`, не дожидаясь публичного HTTPS-адреса из DEPLOY-001 —
меняет приоритет DEPLOY-001 (раздел 9).

Источник: [dev.max.ru/docs-api/methods/GET/updates](https://dev.max.ru/docs-api/methods/GET/updates).

## 9. Ник бота и создание бота

**Подтверждено документацией и живым запросом 22.09.2026.** Бот
создаётся через `@MasterBot` в самом MAX: команда `/create`, ник 11–60
символов, обязательно оканчивается на `_bot`. Токен выдаётся в момент
создания.

**Организаторы хакатона уже создали бота вместе с токеном** — не только
токен, но и сам бот с ником уже существовал до начала работы над
проектом. Подтверждено запросом `GET /me`:

| Поле | Значение |
|---|---|
| `username` (ник, сменить нельзя — ограничение №11 кейса) | `t449_hakaton_max_bot` |
| `first_name` (отображаемое имя) | «Хакатон МАХ 449» — заготовка организаторов, стоит заменить на своё |
| `user_id` | `426772098` |
| `description` | заготовка организаторов, стоит заменить |

## 10. Не подтверждено / не применимо к API

- **Различия мобильной и веб-версии MAX** — это поведение клиента
  (рендеринг), а не контракт API; официальная документация API его не
  описывает. Проверяется вручную в самом MAX после DEPLOY-001, не здесь.
- **Мини-приложения (MAX Bridge, требования к HTTPS для web_app)** —
  за пределами Bot API (`dev.max.ru/docs-api`); отдельный раздел
  `dev.max.ru/docs` для MAX UI/mini apps, не исследовался в рамках
  MAX-001 (не нужен для Must Have, актуально для WEB-001 при выборе
  Could Have).
- **Точные лимиты частоты запросов (rate limit) к `/messages`** — на
  просмотренных страницах не зафиксированы явным числом; проверить
  отдельно перед EVAL-002/демо, если объём тестовых сообщений будет большим.
- **Вложения-фото** — тип `attachment` в `MessageBody` подтверждён
  структурно (`attachments` массив), но конкретный формат загрузки фото
  (`POST /uploads` и связка с сообщением) не изучался — не нужен для
  Must Have, актуально для PHOTO-001 (Could Have).

## 11. Живая проверка

**Выполнена полностью 22.09.2026**, на публичном деплое (DEPLOY-001,
`https://digital-upravdom.duckdns.org`), с явного подтверждения
пользователя перед первым исходящим вызовом:

- `GET /me` — `200`, бот подтверждён (раздел 9);
- `POST /subscriptions` — вебхук зарегистрирован на
  `https://digital-upravdom.duckdns.org/webhook/max`, `update_types`:
  `message_created`, `bot_started`, `message_callback`;
- `GET /subscriptions` — подписка подтверждена активной;
- реальное TLS-соединение с `platform-api2.max.ru` проверено из **самого
  контейнера приложения** (не только с хоста) — обнаружена и исправлена
  реальная проблема с доверенным ЦС (раздел 3);
- **реальный диалог**: пользователь открыл `t449_hakaton_max_bot` в MAX
  и написал сообщения — пришли 3 события (`bot_started` + 2×
  `message_created`), все обработаны с первой попытки (`inbound_events.
  status = done`, 0 ретраев), на оба `message_created` бот ответил через
  реальный `POST /messages` (`outbound_messages.status = sent`, 0
  ошибок); на `bot_started` бот корректно промолчал (не зарегистрирован
  обработчик — architecture.md §8, дизайн, не баг).

Не проверялся отдельно только long polling (`GET /updates`) — вебхук уже
зарегистрирован и рабочий.

## 12. Итог для платформенного бонуса

**Подтверждено: стартовый параметр deep-link существует и работает** —
формат `https://max.ru/<ник>?start=<payload>`, доставляется в
`bot_started.payload`. Платформенный бонус (idea_and_scope.md, шаг 7)
обоснован документацией, не только предположением.

---

## История сверки

**[22.09.2026] Первая версия.** Составлена по `dev.max.ru/docs-api` и
`api-schema/schema.yaml` (ветка `main`). Найдены содержательные
расхождения с реализацией BOT-001 (заголовок и механизм подписи вебхука,
формат `Authorization`, отсутствие `update_id`/ключ идемпотентности через
`message.body.mid`, реальная схема `Update`) — правки внесены в
`architecture.md` и в код `bot_gateway/` отдельным проходом сразу после
этого документа.
