# DATA-001 — Детерминированное демо-состояние

| Поле | Значение |
|---|---|
| ID | `DATA-001` |
| Статус | `done` |
| Требования | architecture.md §5.1, §5.3, §13; idea_and_scope.md — сценарий пилота (Казань, старый фонд под УК) |
| Зависимости | DB-001 |
| Блокирует | ONBOARD-001, TICKET-001, NOTIFY-001 |

## Цель

Наполнить базу воспроизводимым тестовым состоянием: УК, РСО, дома,
адресаты по ресурсам, deep-link токены на дома и тестовые уведомления —
без этого ONBOARD-001 нечего привязывать (нет ни одного дома), а
TICKET-001 некуда маршрутизировать заявку. Данные — смоделированные,
помечены `is_test_data`, что явно обозначено в README (требование №10
кейса).

## Scope

### Входит

- `scripts/seed_demo.py`: 2 УК, 6 записей РСО (по одной на каждый тип
  ресурса из `problem_types`/`ResourceType`), 3 дома в Казани (старый
  фонд, сегмент из idea_and_scope.md шаг 2), связи `house_resource_orgs`
  на все дома, deep-link токены (`house_links`) с метками «подъезд» и
  «квитанция» на каждый дом, 2 тестовых уведомления (`notifications`);
- идемпотентность через детерминированные UUID (`uuid5` от namespace +
  бизнес-ключ) — повторный запуск не плодит дублей, как в
  `seed_problem_types.py`;
- подключение в `scripts/entrypoint.sh` (после `seed_problem_types.py`)
  — часть требования «запуск одной командой» (кейс, формат сдачи);
- вывод собранных deep-link на дома в лог при старте — иначе тестировщик
  не найдёт готовые ссылки для проверки сценария (architecture.md §13).

### Не входит

- реальные адреса ГИС ЖКХ/ФИАС — `fias_id` остаётся `NULL`, это тестовые
  данные, не привязка к реестру;
- УК/РСО пилотного партнёра — здесь только демо-данные для проверки
  кода, не переговоры с реальной УК (это уровень пилота из
  idea_and_scope.md, не MVP);
- сами заявки (`tickets`) — создаются в TICKET-001/FLOW-001, не здесь.

## Технические требования

**Идемпотентность.** Все сущности получают `id` через
`uuid.uuid5(SEED_NAMESPACE, "<стабильный бизнес-ключ>")` — тот же подход,
что уже подтверждён в `seed_problem_types.py`. Upsert через
`INSERT ... ON CONFLICT (id) DO UPDATE`. Порядок вставки учитывает
внешние ключи: `management_companies`/`resource_organizations` → `houses`
→ `house_resource_orgs`/`house_links` → `notifications`.

**`is_test_data`.** У `management_companies` и `resource_organizations`
это поле обязательно (DB-001 — `NOT NULL` без дефолта) — семя всегда
ставит `True` явно.

**Данные — правдоподобные, не случайные.** Дома в Казани (продолжают
логику пилота из idea_and_scope.md: Казань — первая цель
масштабирования), региональный код `RU-TA`. Контактные телефоны УК/РСО —
заведомо тестовые номера с узнаваемым паттерном (`+7 (843) 000-00-0X`),
чтобы их нельзя было спутать с настоящими при демонстрации.

**Deep-link токены.** Короткие, только ASCII (ограничение MAX-001:
`payload` ≤128 символов, без спецсимволов/кириллицы) — `secrets.
token_urlsafe`-подобный формат, но детерминированный (от того же `uuid5`,
взятого в hex).

**Уведомления.** `source = 'test_data'`, времена — относительно момента
сидирования (`now() + interval`), чтобы при демонстрации выглядели
актуальными независимо от даты запуска.

## Артефакты

| Артефакт | Назначение |
|---|---|
| `scripts/seed_demo.py` | Идемпотентный сидер демо-состояния |
| `scripts/entrypoint.sh` | Подключение сидера после миграций и `problem_types` |
| `tests/integration/test_seed_demo.py` | Идемпотентность, `is_test_data`, ссылочная целостность |

## Критерии приёмки

- [x] Повторный запуск `seed_demo.py` не создаёт дублей (число строк в каждой таблице не меняется).
- [x] У всех `management_companies`/`resource_organizations` `is_test_data = true`.
- [x] Каждый дом имеет привязку по всем 6 типам ресурса (`house_resource_orgs`).
- [x] Каждый дом имеет минимум 2 действующих deep-link токена (`house_links.is_active = true`).
- [x] Deep-link токены проходят ограничения MAX-001 (≤128 символов, только ASCII без спецсимволов, требующих URL-кодирования).
- [x] `docker compose up` из чистого состояния наполняет демо-данные автоматически (без ручных шагов).
- [x] `bash scripts/check.sh` проходит.

## Проверка

```bash
docker compose up -d postgres
uv run python scripts/seed_demo.py
uv run python scripts/seed_demo.py   # повторный запуск — без ошибок и дублей
uv run pytest tests/integration/test_seed_demo.py
bash scripts/check.sh
```

## Результат закрытия

Закрыто 22.09.2026.

**Сделано:**

- `scripts/seed_demo.py` — идемпотентный сидер: 2 УК, 6 РСО (по одной
  записи на каждый `ResourceType`), 3 дома в Казани (`RU-TA`), связи
  `house_resource_orgs` на все 3 дома × 6 типов ресурса (18 строк),
  `house_links` с метками «подъезд»/«квитанция» на каждый дом (6 строк,
  все `is_active = true`), 2 тестовых `notifications` (плановое и
  аварийное отключение). Все id — детерминированный `uuid5(SEED_NAMESPACE,
  "<бизнес-ключ>")`, upsert через `pg_insert(...).on_conflict_do_update`.
- `scripts/entrypoint.sh` — подключён вызов `seed_demo.py` после
  `seed_problem_types.py`.
- `tests/integration/test_seed_demo.py` — 5 тестов: идемпотентность (число
  строк не меняется при повторном запуске), `is_test_data = true` у всех
  организаций, полное покрытие ресурсов на каждый дом, ≥2 активных
  deep-link на дом, формат токена (ASCII, ≤128 символов).
- `pyproject.toml` — `pythonpath` дополнен `.` (сидер не пакет
  `upravdom`, а отдельный `scripts/`, тесты импортируют его напрямую).

**Найденный и исправленный баг:** `docker/app.Dockerfile` копировал в
финальный образ только `entrypoint.sh` и `seed_problem_types.py`
(`COPY scripts/entrypoint.sh scripts/seed_problem_types.py ./scripts/`)
— `seed_demo.py` не попадал в образ вообще, из-за чего `docker compose up`
падал бы на старте контейнера (`entrypoint.sh` вызывает несуществующий в
образе файл). Обнаружено живой проверкой критерия «`docker compose up`
из чистого состояния наполняет демо-данные автоматически» — первая
пересборка образа завершилась с exit code 1. Исправлено добавлением
`seed_demo.py` в ту же `COPY`-строку, пересобрано и перепроверено.

**Фактические проверки:**

- `uv run python scripts/seed_demo.py` дважды подряд локально (venv +
  `docker compose up -d postgres`) — одинаковые детерминированные
  deep-link токены на обоих запусках, без ошибок.
- Прямой подсчёт строк в БД после сидирования: `management_companies` = 2,
  `resource_organizations` = 6, `houses` = 3, `house_resource_orgs` = 18,
  `house_links` = 6, `notifications` = 2 — совпадает с ожиданием, дублей
  нет.
- `uv run pytest tests/integration/test_seed_demo.py -v` — 5/5 passed.
- `bash scripts/check.sh` — все 6 проверок пройдены (`ruff check`,
  `ruff format --check`, `mypy --strict`, `pytest` — 71/71,
  `docker compose config`, секреты).
- `docker compose up -d --build app` с чистой пересборкой образа —
  логи контейнера подтверждают: миграции → `seed_problem_types` →
  `seed_demo` (с выводом всех 6 deep-link в лог) → старт приложения;
  `/health` и `/ready` отвечают `200 OK` после рестарта.

**Отклонения от постановки:** нет.

**Follow-up:** нет — блокирует ONBOARD-001, TICKET-001, NOTIFY-001,
можно начинать.
