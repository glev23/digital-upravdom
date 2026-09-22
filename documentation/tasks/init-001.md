# INIT-001 — Воспроизводимый каркас репозитория

| Поле | Значение |
|---|---|
| ID | `INIT-001` |
| Статус | `done` |
| Требования | architecture.md §1, §2, §8, §11, §12; кейс — «Формат сдачи», пп. 2–5 |
| Зависимости | DOCS-001 |
| Блокирует | SPIKE-001, DB-001, MASK-001, LLM-001, DEPLOY-001 и все задачи реализации |

## Цель

Создать минимальный каркас приложения, который собирается и поднимается одной
командой `docker compose up` вместе с PostgreSQL и Qdrant, имеет
зафиксированные зависимости, безопасную конфигурацию и единую команду
проверки — без бизнес-логики.

Каркас сразу закрывает формальные требования формата сдачи (Git-репозиторий,
файл зависимостей, Dockerfile, `compose.yaml`, `.dockerignore`, `.env.example`),
чтобы к ним не возвращаться в последний день.

## Scope

### Входит

- инициализация Git (проект сейчас не является репозиторием, а кейс требует
  Git с commit hash);
- декларация зависимостей в `pyproject.toml` и lock-файл `uv.lock`;
- пакет `src/upravdom/` с пустыми модулями по границам architecture.md §8;
- конфигурация из окружения с валидацией при старте;
- FastAPI-приложение с `GET /health` и `GET /ready`;
- заготовка Alembic без таблиц;
- Dockerfile приложения и `compose.yaml` с `app`, `postgres`, `qdrant`;
- стартовый скрипт контейнера (миграции → запуск приложения);
- единая команда проверки `scripts/check.sh`;
- минимальные тесты каркаса;
- раздел «Быстрый старт» в корневом README с реально выполненными командами;
- обновление README-заглушек в `src/`, `db/`, `docker/`, `scripts/`, `tests/`.

### Не входит

- таблицы и модели данных (DB-001);
- эмбеддинги, `sentence-transformers`, `torch`, ONNX — выбор рантайма
  делает SPIKE-001, в каркас эти зависимости не добавляются;
- интеграция с MAX Bot API (MAX-001, BOT-001);
- клиент OpenRouter (LLM-001);
- мини-приложение `web/` (WEB-001);
- публичный HTTPS и регистрация вебхука (DEPLOY-001);
- CI во внешнем сервисе.

## Технические требования

**Зависимости.** Python 3.12, менеджер `uv`. Основные: `fastapi`,
`uvicorn[standard]`, `pydantic-settings`, `sqlalchemy[asyncio]` 2.x,
`asyncpg`, `alembic`, `qdrant-client`, `httpx`, `apscheduler`. Для разработки:
`ruff`, `mypy`, `pytest`, `pytest-asyncio`. Версии фиксируются в `uv.lock`;
сборка образа использует `uv sync --frozen` и падает при расхождении lock-файла
с `pyproject.toml`.

**Структура пакета.** `src/` — технический корень, `src/upravdom/` —
единственный импортируемый пакет. Модули-заготовки: `bot_gateway`,
`onboarding`, `classifier`, `dedup`, `tickets`, `notifications`, `knowledge`
(architecture.md §8), плюс `config.py`, `db.py`, `main.py`. Модули содержат
только `__init__.py` — без кода «на будущее».

**Конфигурация.** Все параметры читаются из окружения через
`pydantic-settings` по составу `.env.example`. Приложение не стартует при
отсутствии обязательных параметров хранилищ и сообщает, какого именно
параметра не хватает, **не печатая значения**. Параметры внешних сервисов
(`MAX_BOT_TOKEN`, `OPENROUTER_API_KEY`) на этом этапе необязательны — их
отсутствие не должно мешать поднять каркас без токенов.

**Health и readiness.**
- `GET /health` — процесс жив, без обращения к хранилищам, `200 {"status": "ok"}`.
- `GET /ready` — проверяет доступность PostgreSQL и Qdrant; `200`, если оба
  доступны, иначе `503` с перечнем недоступных компонентов без деталей
  подключения.

**Docker.**
- `docker/app.Dockerfile` на `python:3.12-slim`; слой зависимостей
  (`pyproject.toml` + `uv.lock`) копируется и устанавливается **до** исходников,
  чтобы правка кода не пересобирала зависимости.
- `compose.yaml` — **в корне репозитория**: проверяющий запускает
  `docker compose up` без флага `-f`.
- `postgres:16` и `qdrant/qdrant:v1.17.0` (тег зафиксирован — см. ниже) с
  healthcheck и именованными томами `postgres_data`, `qdrant_data`
  (architecture.md §12).
- **Решение изменено при реализации (ревизия 4 architecture.md, §11, §12.1):**
  порты PostgreSQL и Qdrant публикуются на хост нестандартными значениями
  (`POSTGRES_PORT=55432`, `QDRANT_PORT=56333`) — нужны локальному
  venv-процессу для дев-воркфлоу. Контейнер `app` при этом обращается к
  соседям по имени сервиса и внутреннему порту через явный `environment:`
  в `compose.yaml`, который имеет приоритет над `env_file:` — оба сценария
  работают из одного `.env` без переключения между ними.
- Тег образа Qdrant зафиксирован (`v1.17.0`, не `latest`), а диапазон версий
  `qdrant-client` в `pyproject.toml` ограничен сверху под него — иначе клиент
  предупреждает о несовместимости major/minor при расхождении версий.
- `app` стартует после `service_healthy` обоих хранилищ.
- Стартовый скрипт `scripts/entrypoint.sh`: `alembic upgrade head` → `uvicorn`.
  Точки для сидирования и загрузки векторов оставляются закомментированными
  с указанием задач (DATA-001, KB-001).
- Переменные окружения передаются через `env_file: .env`; секреты в образ не
  попадают (`.dockerignore` уже настроен allowlist-стилем).

**Единая проверка `scripts/check.sh`.** Завершается с ненулевым кодом при
любом нарушении и выполняет:

1. `uv lock --check` — lock-файл соответствует `pyproject.toml`;
2. `ruff check` и `ruff format --check`;
3. `mypy` в strict-режиме для `src` и `tests`;
4. `pytest`;
5. `docker compose config --quiet`;
6. проверку, что в отслеживаемых файлах нет `.env`, ключей и строк, похожих
   на токены (`MAX_BOT_TOKEN=` и `OPENROUTER_API_KEY=` с непустым значением).

## Артефакты

| Артефакт | Назначение |
|---|---|
| `.git/` | Репозиторий, первый коммит с каркасом |
| `pyproject.toml`, `uv.lock` | Зависимости и настройки ruff / mypy / pytest |
| `src/upravdom/config.py` | Валидируемая конфигурация из окружения |
| `src/upravdom/main.py` | Фабрика FastAPI, `/health`, `/ready` |
| `src/upravdom/db.py` | Асинхронный движок SQLAlchemy и сессии |
| `src/upravdom/{bot_gateway,onboarding,classifier,dedup,tickets,notifications,knowledge}/` | Пустые пакеты по границам модулей |
| `db/alembic.ini`, `db/migrations/` | Заготовка Alembic без ревизий с таблицами |
| `docker/app.Dockerfile` | Образ приложения |
| `compose.yaml` | app + postgres + qdrant одной командой |
| `scripts/entrypoint.sh` | Миграции и запуск при старте контейнера |
| `scripts/check.sh` | Единая проверка проекта |
| `tests/test_health.py`, `tests/test_config.py` | Тесты каркаса |
| `README.md` | Раздел «Быстрый старт» |

## Критерии приёмки

- [ ] Проект — Git-репозиторий; первый коммит не содержит `.env` и секретов. *(`git init` выполнен; сам коммит — по решению пользователя, см. «Результат закрытия»)*
- [x] На чистой машине `cp .env.example .env && docker compose up --build` поднимает три контейнера без ручных шагов.
- [x] `GET /health` возвращает `200`, `GET /ready` возвращает `200` при живых хранилищах.
- [x] При остановленном Qdrant `GET /ready` возвращает `503` и называет недоступный компонент.
- [x] Отсутствие обязательного параметра окружения даёт понятную ошибку старта без печати значений.
- [x] `docker compose down` → `docker compose up` проходит без ошибок, тома сохраняются.
- [x] **Изменено при реализации:** PostgreSQL и Qdrant **доступны** с хоста напрямую (нестандартные порты, дев-воркфлоу через venv — architecture.md §12.1), вместо исходного «недоступны». Причина и разбор риска — там же.
- [x] Время холодной сборки образа замерено и записано в результат закрытия — это baseline для SPIKE-001.
- [x] `bash scripts/check.sh` проходит; искусственное нарушение (ошибка ruff, упавший тест) даёт ненулевой код.
- [x] README содержит только реально выполненные команды.

## Проверка

```bash
bash scripts/check.sh
```

```bash
cp .env.example .env
docker compose up --build -d
curl -s http://localhost:8000/health
curl -s http://localhost:8000/ready
docker compose stop qdrant && curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/ready
docker compose down && docker compose up -d
```

Ожидаемо: `{"status":"ok"}`, затем `200` от `/ready`, затем `503` после
остановки Qdrant, затем успешный повторный подъём.

Замер холодной сборки (базовый образ скачан заранее, кэш сборки очищен):

```bash
docker pull python:3.12-slim
docker builder prune -af
time docker compose build --no-cache app
```

## Результат закрытия

Закрыто 21.09.2026.

**Созданные артефакты:**

- `git init` выполнен; коммит не сделан — по общему правилу «коммитить
  только по явному запросу пользователя», не по недосмотру.
- `pyproject.toml` + `uv.lock`: зависимости из architecture.md §2 (без
  `sentence-transformers`/`torch` — они входят в SPIKE-001, не в INIT-001);
  `[build-system]` на hatchling, пакет собирается из `src/upravdom`.
- `src/upravdom/{config.py, db.py, vector_store.py, main.py}` и пустые
  модули `bot_gateway/`, `onboarding/`, `classifier/`, `dedup/`, `tickets/`,
  `notifications/`, `knowledge/` по границам §8.
- `db/alembic.ini`, `db/migrations/env.py` (асинхронный шаблон),
  `db/migrations/script.py.mako`, пустой `db/migrations/versions/`.
- `docker/app.Dockerfile`, `compose.yaml` (в корне), `scripts/entrypoint.sh`.
- `scripts/check.sh` — ruff, mypy, pytest, `compose config`, проверка секретов
  (по файлам, которые git будет отслеживать, а не только по уже
  застейдженным — иначе проверка до первого `git add` ничего не находит).
- `tests/unit/test_main.py`, `tests/unit/test_config.py` — 7 тестов.
- Обновлены README: корневой (раздел «Быстрый старт», два сценария запуска),
  `src/`, `db/`, `docker/`, `scripts/`, `tests/`.

**Отклонения от постановки, зафиксированные в architecture.md (ревизия 4) и
здесь же в разделах Scope/Технические требования/Критерии приёмки:**

- Порты PostgreSQL/Qdrant публикуются на хост (`55432`, `56333`) вместо
  исходного «не публикуются» — по прямому запросу пользователя для
  дев-воркфлоу через venv. Причина, риск и его граница — architecture.md §12.1.
- `.env`/`.env.example` физически обнаружились в `credentials/` вместо
  корня (расхождение с §12 из более раннего этапа работы) — возвращены в
  корень как канонические.
- Тег образа Qdrant зафиксирован (`v1.17.0`), диапазон `qdrant-client`
  ограничен сверху — иначе на прогретом кэше `uv sync` подтягивал клиент
  новее сервера и печатал предупреждение о несовместимости.

**Фактические проверки (все выполнены вживую, не только описаны):**

- `bash scripts/check.sh` — `OK`, все 6 проверок пройдены.
- Обратная проверка: искусственный ruff-нарушение → `check.sh` упал с кодом
  `1`; после отката снова `OK`.
- `docker compose config --quiet` — валиден.
- **Холодная сборка** (`docker builder prune -af` + `docker compose build
  --no-cache app`, базовый образ `python:3.12-slim` скачан заранее, как
  требует кейс): **≈66 секунд**. Это baseline без эмбеддингов — SPIKE-001
  замеряет бюджет уже с `torch`/ONNX поверх этого числа.
- `docker compose up -d --build`: все три контейнера стартуют,
  `postgres`/`qdrant` — `healthy` до старта `app`.
- `GET /health` → `200 {"status":"ok"}`.
- `GET /ready` → `200 {"status":"ok","postgres":true,"qdrant":true}`.
- Остановка `qdrant` → `GET /ready` → `503`,
  `{"status":"unavailable","postgres":true,"qdrant":false}`; после
  перезапуска `qdrant` — снова `200`.
- `docker compose down` → `docker compose up -d`: поднимается без ошибок,
  `/health` и `/ready` снова `200` (тома `postgres_data`/`qdrant_data`
  сохранены).
- Порты хоста подтверждены: `docker port` показывает `55432→5432` и
  `56333→6333`; TCP-подключение с хоста на оба порта успешно.
- **Дев-воркфлоу через venv подтверждён отдельно:** `uv run alembic -c
  db/alembic.ini current` — подключился к контейнерному PostgreSQL через
  `localhost:55432` без ошибок; локально запущенный (не в контейнере)
  `uvicorn upravdom.main:app` на порту 8001 отдал `200` на `/health` и
  `/ready`, обращаясь к тем же контейнерам через хостовые порты.
- Отсутствие обязательного параметра окружения: тест
  `test_settings_require_database_url`/`..._qdrant_url` — `ValidationError`
  без утечки значений остальных переменных.
- `mypy --strict` — 14 файлов, 0 ошибок; `ruff check` и `ruff format --check`
  — чисто.

**Follow-up задачи:**

- `SPIKE-001` и `DB-001` переведены в `ready` — их зависимость (INIT-001)
  закрыта, обе спецификации самодостаточны (см. `progress_dev.md`).
- Коммит первой версии — по запросу пользователя, вне рамок этой задачи.
