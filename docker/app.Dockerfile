# Образ приложения "Цифровой Управдом": webhook, воркер и планировщик
# в одном процессе (architecture.md §1). CPU-only, без CUDA (architecture.md §2).
#
# Два стейджа: `exporter` тянет torch и экспортирует модель в ONNX внутри
# сборки — веса модели НЕ хранятся в репозитории (490 МБ превышают лимит
# GitHub 100 МБ/файл без Git LFS, см. "История решений" architecture.md).
# `base` копирует только результат экспорта; torch/optimum в финальный
# образ не попадают — рантайм не тянет их вообще (SPIKE-001).

# ---------- exporter: только чтобы получить ONNX, слой отбрасывается ----------
FROM python:3.12-slim AS exporter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv-export

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /export
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --group embedding-export

COPY scripts/export_onnx.py ./scripts/export_onnx.py
ENV PATH="/opt/venv-export/bin:${PATH}"
RUN python scripts/export_onnx.py

# ---------- base: рантайм-образ, без torch ----------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Российский удостоверяющий центр (Минцифры), которым подписан сертификат
# platform-api2.max.ru — подтверждено живым запросом 22.09.2026 (без этого
# httpx.AsyncClient в HttpxMaxClient получает "unable to get local issuer
# certificate": стандартный набор доверенных ЦС в python:3.12-slim и в
# certifi (которым по умолчанию пользуется httpx) этот корневой центр не
# включает). Файлы — публичные сертификаты, не секреты; источник —
# gu-st.ru (официальный ресурс Минцифры), см. max_api.md.
COPY docker/certs/russian_trusted_root_ca.pem /usr/local/share/ca-certificates/russian-trusted-root-ca.crt
COPY docker/certs/russian_trusted_sub_ca.pem /usr/local/share/ca-certificates/russian-trusted-sub-ca.crt
RUN update-ca-certificates

WORKDIR /app

# --- Слой зависимостей (кэшируется, пока не меняются pyproject.toml/uv.lock) ---
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# --- Модель эмбеддингов — результат стейджа exporter, не из репозитория ---
COPY --from=exporter /export/data/model ./data/model

# --- Исходники, миграции и справочники (меняются часто — отдельный слой) ---
COPY src ./src
COPY db ./db
COPY data/seed ./data/seed
COPY data/kb ./data/kb
COPY data/masking ./data/masking
COPY data/classifier ./data/classifier
COPY scripts/entrypoint.sh scripts/seed_problem_types.py scripts/seed_demo.py \
     scripts/load_kb.py scripts/reindex.py scripts/build_kb.py \
     scripts/build_prototypes.py scripts/ticket_status.py scripts/ticket_reroute.py \
     scripts/notify.py ./scripts/

RUN uv sync --frozen --no-dev

ENV PATH="/opt/venv/bin:${PATH}"

EXPOSE 8000

ENTRYPOINT ["bash", "scripts/entrypoint.sh"]
