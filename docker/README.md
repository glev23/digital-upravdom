# docker

- `app.Dockerfile` — образ приложения (`python:3.12-slim`, `uv`). Слой
  зависимостей копируется и ставится до исходников — правка кода не
  пересобирает `uv.lock`.

`compose.yaml` для всех контейнеров (app/postgres/qdrant) находится **в
корне репозитория**, не здесь — так его находит `docker compose up` без
флага `-f`, и там же его ожидает проверяющий (формат сдачи кейса).

Собрать образ отдельно:

```bash
docker compose build app
```

См. [architecture.md §12](../documentation/architecture.md) — Docker-конфигурация,
в т.ч. §12.1 — почему у `app` и локального venv-процесса разные адреса
хранилищ.
