# tests

Автотесты (`pytest`, конфигурация в `pyproject.toml`).

- `unit/` — без сети и без реальных хранилищ (готовность PostgreSQL/Qdrant
  подменяется через `monkeypatch`).
- `integration/` — с реальным PostgreSQL/Qdrant, появится вместе с DB-001.

```bash
uv run pytest
```

или в составе единой проверки — `bash scripts/check.sh`.
