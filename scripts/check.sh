#!/usr/bin/env bash
# Единая проверка проекта (INIT-001, documentation/tasks/init-001.md).
# Завершается с ненулевым кодом при любом нарушении.
set -uo pipefail

status=0
fail() {
    echo "FAIL: $1" >&2
    status=1
}

echo "== 1/6 uv lock --check =="
if ! uv lock --check; then
    fail "uv.lock не соответствует pyproject.toml"
fi

echo "== 2/6 ruff check =="
if ! uv run ruff check src tests; then
    fail "ruff check нашёл нарушения"
fi

echo "== 2/6 ruff format --check =="
if ! uv run ruff format --check src tests; then
    fail "код не отформатирован ruff format"
fi

echo "== 3/6 mypy (strict) =="
if ! uv run mypy src tests; then
    fail "mypy нашёл ошибки типов"
fi

echo "== 4/6 pytest =="
if ! uv run pytest; then
    fail "тесты не прошли"
fi

echo "== 5/6 docker compose config =="
if ! docker compose config --quiet; then
    fail "compose.yaml невалиден"
fi

echo "== 6/6 секреты в отслеживаемых файлах =="
# Ищем непустые значения MAX_BOT_TOKEN / OPENROUTER_API_KEY / MAX_WEBHOOK_SECRET
# среди файлов, которые git реально отслеживает (или отследит при добавлении) —
# т.е. не в .env, credentials/ и прочем, что уже в .gitignore.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # --cached --others --exclude-standard: и уже добавленное, и то, что
    # добавится при `git add .` — но не игнорируемое. Проверка секретов
    # должна ловить утечку ДО первого коммита, а не только после `git add`.
    tracked_hits=$(git ls-files -z --cached --others --exclude-standard \
        | xargs -0 grep -lE '^(MAX_BOT_TOKEN|MAX_WEBHOOK_SECRET|OPENROUTER_API_KEY|LLM_PROXY)=.+' 2>/dev/null \
        | grep -v '\.env\.example$' || true)
    if [ -n "$tracked_hits" ]; then
        fail "похоже на секрет в отслеживаемом файле: $tracked_hits"
    fi
else
    echo "не git-репозиторий или git недоступен — пропуск проверки отслеживаемых файлов"
fi
if [ -f .env ] && ! git check-ignore -q .env 2>/dev/null; then
    fail ".env не игнорируется git"
fi

echo
if [ "$status" -eq 0 ]; then
    echo "OK: все проверки пройдены"
else
    echo "ЕСТЬ НАРУШЕНИЯ — см. вывод выше"
fi
exit "$status"
