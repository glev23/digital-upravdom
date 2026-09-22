# TICKET-001 — Заявки: создание, адресат, нормативный срок, история статусов

| Поле | Значение |
|---|---|
| ID | `TICKET-001` |
| Статус | `done` |
| Требования | architecture.md §5.2 (`tickets`, `ticket_events` append-only, `ticket_subscribers`), §5.1 (`house_resource_orgs`), §4 (`problem_types.resolution_hours`), §8 (модуль `tickets`, решение по собственному API), §13 (статус по номеру, перезапуск); idea_and_scope.md To Be п. 2–3 |
| Зависимости | DB-001, DATA-001 |
| Блокирует | FLOW-001, STATUS-001, DEDUP-001 |

## Цель

По решению классификатора создаётся заявка с коротким номером, который
житель может назвать и запомнить, с конкретным адресатом (УК дома или
РСО по нужному ресурсу), нормативным сроком из справочника и
неизменяемой историей переходов. Статус проверяется по номеру — но
только тем, кто на заявку подписан. Это второе ценностное обещание
продукта после «кому»: заявка с цифровым следом вместо устной передачи
по телефону.

## Scope

### Входит

- модуль `src/upravdom/tickets/`: создание, определение адресата, расчёт
  `due_at`, переходы статусов, чтение по номеру с проверкой доступа;
- человекочитаемый номер заявки (миграция);
- закрытый перечень типов событий `ticket_events`;
- автор заявки — первый подписчик (`ticket_subscribers`, `is_author`);
- служебная смена статуса для демонстрации — CLI-скрипт, а не HTTP;
- тексты статусов для жителя.

### Не входит

- диалог в MAX и решение, по каким зонам создавать заявку, — FLOW-001;
- уведомление подписчиков о переходах — STATUS-001;
- склейка дубликатов и статус `merged` — DEDUP-001 (переход в `merged`
  здесь запрещён);
- рабочее место диспетчера УК — ограничение MVP (architecture.md §15).

## Технические требования

**Номер заявки.** `tickets.number` — `BIGINT` из последовательности
`ticket_number_seq`, `UNIQUE NOT NULL`, выдаётся базой при вставке.
Жителю показывается как `№ 1024`. UUID в интерфейсе не показывается
никогда: его нельзя продиктовать по телефону диспетчеру. Последовательность
глобальная, а не на УК — проще, и номер не раскрывает объём заявок
конкретной УК. Миграция `0005`.

**Типы событий** — `TicketEventType` (StrEnum) + CHECK на
`ticket_events.event_type` в той же миграции: `created`,
`status_changed`, `routed`, `subscriber_joined`. Свободная строка
позволяла записать что угодно и сломать ленту истории, которую увидит
житель.

**Создание** — одна транзакция: строка `tickets` (`status` — `accepted`,
для зоны `unknown` — `needs_dispatcher`), событие `created` в
`ticket_events`, автор в `ticket_subscribers` (`join_reason = author`).
Идемпотентность по `source_event_id`: повторная обработка того же
входящего события (ретрай воркера) возвращает уже созданную заявку, а не
вторую — частичный уникальный индекс `tickets(source_event_id) WHERE
source_event_id IS NOT NULL` в миграции `0005`.

**Адресат** — `resolve_addressee(house_id, zone, problem_type, on=…)`:

| Зона | Адресат | Контакт для жителя |
|---|---|---|
| `uk` | `houses.management_company_id` | `management_companies.ads_phone`, `ads_hours` |
| `rso` | `house_resource_orgs` по ресурсу, действующая на дату (`valid_from ≤ on < valid_to` или `valid_to IS NULL`) | `resource_organizations.contact` |
| `unknown` | УК дома (разбор диспетчером) | телефон АДС УК |
| `owner`, `municipality` | нет | — |

Соответствие `problem_type → resource_type` — явная таблица в коде
(`cold_water`, `hot_water`, `heating`, `sewage`, `electricity`, `gas`
совпадают по коду). Для типов без ресурса (`elevator`, `roof_leak`, …) и
зоны `rso` адресат не определяется → заявка уходит на УК с событием
`routed` и пометкой причины в `payload`, а не падает. Дом без УК
(`management_company_id IS NULL`) — адресат `None`, это отдельный
результат, с которым разбирается FLOW-001.

**Нормативный срок.** `due_at = created_at + problem_types.resolution_hours`.
Если `resolution_hours IS NULL` — `due_at IS NULL`, и житель видит
честное «нормативный срок для этого типа не установлен» со ссылкой на
норму, а не выдуманную цифру (принцип справочника DB-001). Уточнено после
ревью: `resolution_hours` — это **допустимая продолжительность перерыва**
(прил. 1 ПП №354), а не срок устранения; FLOW-001 так её и называет.

**Переходы статусов** — таблица разрешённых переходов в коде, любой
другой — исключение, событие не пишется:

| Из | В |
|---|---|
| `accepted` | `in_progress`, `routed_to_contractor`, `completed` |
| `needs_dispatcher` | `accepted`, `in_progress` |
| `in_progress` | `routed_to_contractor`, `completed` |
| `routed_to_contractor` | `in_progress`, `completed` |
| `completed` | — (терминальный) |

`merged` — только через DEDUP-001. Смена статуса = `UPDATE tickets.status,
updated_at` + событие `status_changed` (`from_status`, `to_status`,
`actor`) в одной транзакции. `ticket_events` защищены от `UPDATE/DELETE`
триггером (DB-001) — тест это подтверждает на уровне сервиса.

**Доступ по номеру.** `get_for_user(number, user_id)` возвращает заявку
только подписчику. Номера последовательные, их легко перебрать: без этой
проверки любой житель читал бы чужие адреса и описания проблем. Чужой и
несуществующий номер неразличимы для вызывающего (одинаковый ответ
«заявка не найдена»). Список заявок жителя — `list_for_user` (открытые
первыми, не больше 5).

**Служебная смена статуса** — `scripts/ticket_status.py <номер> <статус>
[--actor dispatcher]`, запускается через `docker compose exec app`. Не HTTP:
по architecture.md §8 объявленный API порождает обязательства сдачи
(openapi, тестовые учётки, проверки), а для демонстрации достаточно
команды в контейнере.

**Тексты статусов** — `tickets/texts.py`: `accepted` — «принята»,
`needs_dispatcher` — «передана диспетчеру для уточнения ответственного»,
`in_progress` — «в работе», `routed_to_contractor` — «передана
подрядчику», `completed` — «выполнена».

## Артефакты

| Артефакт | Назначение |
|---|---|
| `db/migrations/versions/0005_ticket_number_events.py` | `ticket_number_seq`, `tickets.number`, CHECK типов событий, уникальность `source_event_id` |
| `src/upravdom/models/tickets.py`, `enums.py` | `number`, `TicketEventType` |
| `src/upravdom/tickets/service.py` | `create_ticket`, `change_status`, `get_for_user`, `list_for_user` |
| `src/upravdom/tickets/routing.py` | `resolve_addressee`, соответствие `problem_type → resource_type` |
| `src/upravdom/tickets/texts.py` | Тексты статусов |
| `scripts/ticket_status.py` | Служебная смена статуса |
| `tests/integration/test_tickets.py` | Создание, идемпотентность, адресат по зонам, `due_at`, переходы, доступ |

## Критерии приёмки

- [x] Заявка получает уникальный короткий номер; две параллельные вставки не получают одинаковый.
- [x] Повторная обработка того же `source_event_id` не создаёт вторую заявку.
- [x] Адресат верен для `uk`, `rso` (с учётом `valid_from`/`valid_to`), `unknown`; `rso` без ресурса и дом без УК обрабатываются без исключения.
- [x] `resolution_hours IS NULL` → `due_at IS NULL`.
- [x] Запрещённый переход отклоняется и не пишет событие; `merged` недоступен.
- [x] История статусов append-only (попытка изменить событие падает на уровне БД).
- [x] Чужой номер заявки неотличим от несуществующего.
- [x] Заявка, созданная до `docker compose down`, доступна по номеру после `up` (architecture.md §13) — номер и строки в Postgres.
- [x] Миграция `0005` применяется и откатывается.
- [x] `bash scripts/check.sh` проходит.

## Проверка

```bash
uv run alembic -c db/alembic.ini upgrade head
uv run alembic -c db/alembic.ini downgrade -1 && uv run alembic -c db/alembic.ini upgrade head
uv run pytest tests/integration/test_tickets.py -v
docker compose exec app python scripts/ticket_status.py 1 in_progress
bash scripts/check.sh
```

## Результат закрытия

- **Статус:** `done` (22.09.2026), локально без деплоя на VM.
- **Артефакты:** миграция `0005`, `src/upravdom/tickets/`, `scripts/ticket_status.py`.
- **Проверки:** upgrade/downgrade `0005`; 13 integration-тестов заявок;
  `check.sh` — 309 тестов OK.
- **Отклонения:** нет.
- **Follow-up:** FLOW-001 (диалог + когда создавать заявку).
