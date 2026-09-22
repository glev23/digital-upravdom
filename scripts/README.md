# scripts

| Скрипт | Назначение | Статус |
|---|---|---|
| `entrypoint.sh` | Старт контейнера `app`: миграции → запуск приложения | ✅ INIT-001 |
| `check.sh` | Единая проверка проекта (ruff, mypy, pytest, compose config, секреты) | ✅ INIT-001 |
| `seed_demo.py` | Демо-состояние: тестовые УК, РСО, дома, deep-link токены | ⬜ DATA-001 |
| `build_kb.py`, `reindex.py` | Чанкинг нормативов и загрузка векторов в Qdrant | ⬜ KB-001 |
| `run_accuracy_check.py` | Точность классификации против baseline | ⬜ EVAL-002 |
| `run_dedup_check.py` | Доля ложных склеек при дедупликации | ⬜ DEDUP-001 |
| `export_onnx.py`, `spike_embeddings.py` | Замер и выбор рантайма эмбеддингов | ⬜ SPIKE-001 |

Единая проверка проекта:

```bash
bash scripts/check.sh
```

См. [architecture.md](../documentation/architecture.md) и
[progress_dev.md](../documentation/progress_dev.md).
