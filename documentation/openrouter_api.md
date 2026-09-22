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
| `nex-agi/nex-n2.5-mini:free` | structured_outputs + response_format — **основная** |
| `liquid/lfm-2.5-2.6b:free` | structured_outputs + response_format — **резерв** |
| `nex-agi/nex-n2.5-pro:free` | тяжелее mini |
| `nvidia/nemotron-3-super-120b-a12b:free` | крупная, может быть медленной |
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
| JSON | Всегда `temperature=0` + pydantic; `response_format.json_schema` шлём best-effort |
| Нет ключа/модели | `LlmNotConfigured`, приложение стартует |
| Повторы | Нет; только одна попытка резерва |
