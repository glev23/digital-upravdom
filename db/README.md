# db

Миграции Alembic и конфигурация PostgreSQL.

- `alembic.ini` — конфигурация; URL берётся из `upravdom.config.get_settings()`
  (переменная `DATABASE_URL`), а не хранится здесь повторно.
- `migrations/env.py` — асинхронный шаблон Alembic; `target_metadata` пока
  `None` — DB-001 подставит `Base.metadata` из моделей.
- `migrations/versions/` — ревизии. Пока пусто: первая ревизия (19 таблиц
  из architecture.md §5.7) — задача DB-001.

Применить миграции локально:

```bash
uv run alembic -c db/alembic.ini upgrade head
```

См. [architecture.md](../documentation/architecture.md) — раздел «Модель данных».
