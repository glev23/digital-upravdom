# src

Единственный импортируемый пакет — `src/upravdom/`. Модули по границам
architecture.md §8:

| Модуль | Ответственность | Статус |
|---|---|---|
| `config.py` | Настройки из окружения (`pydantic-settings`) | ✅ INIT-001 |
| `db.py` | Асинхронный движок и сессии PostgreSQL | ✅ INIT-001 |
| `vector_store.py` | Клиент Qdrant | ✅ INIT-001 |
| `main.py` | FastAPI-приложение, `/health`, `/ready` | ✅ INIT-001 |
| `bot_gateway/` | Вебхук MAX, inbox/outbox | ⬜ BOT-001 |
| `onboarding/` | Привязка дома, согласие на ПДн | ⬜ ONBOARD-001 |
| `classifier/` | Эмбеддинг, кэш, retrieval, LLM | ⬜ CLASSIFY-001 |
| `dedup/` | Дедупликация массовых обращений | ⬜ DEDUP-001 |
| `tickets/` | Заявки, статусы, история | ⬜ TICKET-001 |
| `notifications/` | Уведомления об отключениях | ⬜ NOTIFY-001 |
| `knowledge/` | База знаний, индексация | ⬜ KB-001 |

См. [architecture.md](../documentation/architecture.md).
