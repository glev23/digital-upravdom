# OpenRouter API — подтверждённый контракт (LLM-001)

**Дата сверки:** 22.09.2026. **Источники:** официальная документация
[openrouter.ai/docs/api-reference/overview](https://openrouter.ai/docs/api-reference/overview),
[Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs),
[Limits](https://openrouter.ai/docs/api-reference/limits),
[FAQ](https://openrouter.ai/docs/faq),
[Zendesk: Rate Limits](https://openrouter.zendesk.com/hc/en-us/articles/39501163636379-OpenRouter-Rate-Limits-What-You-Need-to-Know),
публичный список моделей `GET https://openrouter.ai/api/v1/models`
(снимок 22.09.2026).

Статус: **подтверждено документацией** / **подтверждено живым запросом** /
**не подтверждено** (числа в шаблонах docs без подстановки — уточнены
по FAQ/Zendesk).

---

## 1. Эндпоинт

**Подтверждено документацией.** `POST https://openrouter.ai/api/v1/chat/completions`,
тело OpenAI-совместимое (`messages` / `model` / …).

Источник: [API Reference — overview](https://openrouter.ai/docs/api-reference/overview).

---

## 2. Авторизация

**Подтверждено документацией.** `Authorization: Bearer <OPENROUTER_API_KEY>` —
схема `Bearer` **нужна** (в отличие от MAX Bot API).

Источник: пример fetch в overview.

---

## 3. Необязательные заголовки

**Подтверждено документацией.**

| Заголовок | Назначение |
|---|---|
| `HTTP-Referer` | URL приложения для рейтинга на openrouter.ai |
| `X-OpenRouter-Title` / `X-Title` | Название приложения |

На работу inference не влияют; для демо можно передать
`HTTP-Referer` = `PUBLIC_BASE_URL`, `X-Title` = «Цифровой Управдом».

---

## 4. Структурированный ответ

**Подтверждено документацией.**

- `{ "type": "json_object" }` — JSON без схемы;
- `{ "type": "json_schema", "json_schema": { "name", "strict?", "schema" } }` —
  строгая схема.

Поддержка — **по эндпоинту провайдера**, не только по модели. Чтобы
не уходить на провайдера без поддержки: `provider: { "require_parameters": true }`
и параметр в `supported_parameters`. Даже при поддержке ответ **всегда**
валидируем pydantic на нашей стороне (LLM-001).

Источник: [Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs).

---

## 5. Резервирование моделей

**Подтверждено документацией.** В теле запроса допустимы OpenRouter-only
поля `models: string[]` и `route: "fallback"` — провайдер сам перебирает
модели в одном HTTP-вызове; в ответе `model` — фактически ответившая.

**Решение LLM-001:** резерв делаем **своим вторым вызовом** с
`OPENROUTER_MODEL_FALLBACK` (отдельный таймаут, явный
`fallback_model_used`), а не через `models[]`. Причина: полный контроль
бюджета «не больше двух таймаутов» и однозначная запись в
`classification_logs.model_name` без двусмысленности маршрутизации.

Источник параметра: overview (`models?`, `route?`).

---

## 6. Лимиты бесплатных моделей (`:free`)

**Подтверждено FAQ + Zendesk** (в HTML limits числа подставляются шаблоном
и в снимке страницы пустые — ориентир берём из FAQ/поддержки):

| Условие | RPM | RPD (сутки UTC) |
|---|---|---|
| Куплено кредитов за всё время **&lt; ~$10** | 20 | 50 |
| Куплено **≥ ~$10** | 20 | 1000 |

Лимит суточный — на аккаунт по всем `:free` вместе. При 429:
тело `{ "error": { "code": 429, "message": "…" } }`, заголовки
`X-RateLimit-*` / `Retry-After` на ответе ошибки.

Проверка квоты: `GET /api/v1/key` → `free_model_daily_requests`.

Источники: [FAQ](https://openrouter.ai/docs/faq),
[Zendesk Rate Limits](https://openrouter.zendesk.com/hc/en-us/articles/39501163636379-OpenRouter-Rate-Limits-What-You-Need-to-Know).

**Локальный лимит** приложения (`LLM_MAX_REQUESTS_PER_MINUTE`, стартово 15)
— ниже платформенных 20 RPM, чтобы не упираться в 429 на демо.

---

## 7. Ошибки

**Подтверждено документацией.**

| HTTP | Смысл |
|---|---|
| 401/403 | ключ / доступ |
| 402 | кредиты / in-flight budget |
| 429 | rate limit (платформа или провайдер) |
| 5xx | сбой провайдера/шлюза |

В теле при ошибке: `{ "error": { "code", "message", "metadata?" } }`.
В choice при HTTP 200 возможен `error` внутри choice (нормализация
OpenRouter) — обрабатываем как отказ.

---

## 8. Кандидаты бесплатных моделей (снимок `GET /models`, 22.09.2026)

ID с `pricing=0` и суффиксом `:free`, у которых в `supported_parameters`
есть `structured_outputs` и/или `response_format`:

| ID | Заметки |
|---|---|
| `nex-agi/nex-n2.5-mini:free` | structured_outputs + response_format — **резерв** с 25.09.2026 (§10) |
| `liquid/lfm-2.5-2.6b:free` | structured_outputs + response_format (резерв до CLASSIFY-003) |
| `nex-agi/nex-n2.5-pro:free` | structured_outputs (основная 23–25.09.2026; с 25.09 не отвечает — таймаут/503) |
| `nvidia/nemotron-3-super-120b-a12b:free` | structured_outputs, рассуждающая — **основная** с 25.09.2026 (§10) |
| `qwen/qwen3.8-27b:free` | structured_outputs |
| `google/gemma-4-26b-a4b-it:free` | response_format |
| `openrouter/free` | авто-роутер по бесплатным |

Список `:free` на OpenRouter меняется часто — перед демо сверить
`GET /api/v1/models` и `scripts/llm_smoke.py`. В код ID **не** хардкодятся
(architecture.md §2).

---

## 9. Расхождения / решения для кода

| Тема | Решение |
|---|---|
| Резерв | Второй HTTP-вызов с `OPENROUTER_MODEL_FALLBACK`, не `models[]` |
| JSON | Всегда `temperature=0` + pydantic; `response_format.json_schema` со `strict: true` — схема приводится к строгому виду (`_as_strict_schema`: все поля в `required`, `additionalProperties: false`), см. §10 |
| Нет ключа/модели | `LlmNotConfigured`, приложение стартует |
| Повторы | Нет; только одна попытка резерва |
| Прокси | `LLM_PROXY` — только для этого клиента (с IP пилотной VM openrouter.ai отвечает 403); MAX и Qdrant идут напрямую |
| Таймаут | 45 с на вызов (было 8 с), основная + резерв ≤ 90 с — меньше окна видимости inbound 120 с |

---

## 10. Выбор модели и строгая схема (CLASSIFY-003, 23.09.2026)

**Главная причина «не уверен» была в нашем контракте, а не в модели.**
Pydantic кладёт в `required` только поля без значения по умолчанию, и
`cited_fragments` — основание решения — в схеме был необязательным. Модель
законно его пропускала, а правило CLASSIFY-001 «нет подтверждённой нормы —
нет auto» отправляло обращение в «не уверен». После `_as_strict_schema`
та же `nex-n2.5-mini` на тех же 15 фразах: основание заполнено в **100%**
ответов (было — «чаще пусто»), доля auto — **85%** (было 4 из 15).

Пустой список по-прежнему допустим: обязанность назвать основание не должна
становиться обязанностью его выдумать. Номер вне показанных фрагментов
отбрасывается `confirm_citations`, поэтому жителю по-прежнему не может
попасть несуществующая норма.

**Замер кандидатов** — `scripts/model_bakeoff.py`: промпт как в рабочем
пути (реальный поиск по KB, справочник, маскирование), но без семантического
кэша и с отключённым резервом — иначе повтор возвращал бы сохранённое
решение, а сбой кандидата подменялся бы чужим ответом. 15 фраз
`classify_demo.py`, строгая схема, таймаут 60 с:

| Модель | Ответов | Основание | auto | Медиана |
|---|---|---|---|---|
| `nex-agi/nex-n2.5-pro:free` | **15/15** | 87% | 80% | **8.0 с** |
| `nex-agi/nex-n2.5-mini:free` | 13/15 | 100% | 85% | 13.5 с |
| `nvidia/nemotron-3-super-120b-a12b:free` | 10/15 | 80% | 60% | 7.5 с (на 3 фразах) |
| `qwen/qwen3.8-27b:free` | — | — | — | 429 `upstream_provider_shared_pool` |
| `google/gemma-4-31b-it:free`, `gemma-4-26b-a4b-it:free` | — | — | — | 429 `upstream_provider_shared_pool` |

`auto` — доля ответов с подтверждённым основанием и уверенностью ≥ 0.75,
то есть прошедших бы в автоматическую маршрутизацию по рабочему правилу.

**Решение:**

- **Основная — `nex-agi/nex-n2.5-pro:free`.** Единственная без сбоев
  (15/15) и самая быстрая; доля auto в пределах шума от mini на 15 фразах.
- **Резерв — `nvidia/nemotron-3-super-120b-a12b:free`, у другого
  провайдера.** Вживую (23.09.2026) обе модели Nex AGI одновременно
  перестали отвечать на полный промпт (45 с без содержимого), а Nemotron
  ответил за 21 с. Резерв из того же семейства от отказа провайдера не
  спасает.
- **Не выбраны Qwen и Gemma:** их бесплатные эндпоинты живут в общем пуле
  провайдера и регулярно отвечают 429 `upstream_provider_shared_pool`
  независимо от нашей нагрузки — для основной модели на демо это риск
  доступности, а не качества.

**Задержка по этапам** (медиана на 15 фразах): маскирование ~0 мс,
эмбеддинг 8–9 мс, эмбеддинг + поиск в Qdrant 16 мс, LLM 8.0 с (pro) /
13.5 с (mini). Узкое место — только LLM (architecture.md §14).

**Пересмотр 25.09.2026 — основная и резервная поменялись местами.** Основная
теперь `nvidia/nemotron-3-super-120b-a12b:free`, резерв —
`nex-agi/nex-n2.5-pro:free`. Причина — живой прогон в MAX на пилотной VM:
провайдер Nex AGI не отвечал на полный промпт ни 23.09, ни 25.09 (обе
модели семейства), и каждое обращение ждало полный таймаут основной
модели (45 с) до ответа резервной — ~50 с на ответ жителю. Nemotron за то же
время ответил на все реальные обращения (лифт → `uk`, уверенность 0.95, два
подтверждённых фрагмента). Замер 23.09 (таблица выше) сделан, пока Nex AGI
отвечал; доступность провайдера оказалась важнее разницы в доле auto.
В тот же день резерв сменён на `nex-agi/nex-n2.5-mini:free`: замер с прода
(по 2 вызова без резерва) — Nemotron 1.2–1.6 с, mini 2.2 с, pro — таймаут
и 503, Gemma и Qwen — 429 общего пула провайдера.
