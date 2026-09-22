#!/usr/bin/env bash
# Стартовый скрипт контейнера app: миграции → запуск приложения.
# "docker compose up" обязан поднять систему в рабочее состояние одной
# командой (architecture.md §12) — этот скрипт и есть тот единственный шаг.
set -euo pipefail

echo "[entrypoint] applying migrations..."
alembic -c db/alembic.ini upgrade head

echo "[entrypoint] seeding problem_types..."
python scripts/seed_problem_types.py

echo "[entrypoint] seeding demo data (houses, orgs, deep-links)..."
python scripts/seed_demo.py

echo "[entrypoint] loading knowledge base vectors..."
# Сбой загрузки не валит старт: классификация деградирует (§9/§10).
python scripts/load_kb.py || echo "[entrypoint] WARN: load_kb failed — continuing"

echo "[entrypoint] starting application..."
exec uvicorn upravdom.main:app --host 0.0.0.0 --port 8000
