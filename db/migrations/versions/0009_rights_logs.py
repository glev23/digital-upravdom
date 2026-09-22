"""RIGHTS-001: журнал ответов о правах жителя.

Текст ответа модели не хранится: он может пересказывать вопрос жителя, а для
разбора достаточно ссылок и признака отказа (architecture.md §11).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFUSAL_REASONS = (
    "insufficient",
    "no_citations",
    "unknown_norm_in_answer",
    "llm_unavailable",
    "kb_unavailable",
)
# Длина VARCHAR у enum-колонки = длине самого длинного значения (урок DB-001:
# `awaiting_consent` не влез в VARCHAR(10)).
_REASON_LENGTH = max(len(value) for value in _REFUSAL_REASONS)


def upgrade() -> None:
    op.create_table(
        "rights_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "inbound_event_id",
            sa.Uuid(),
            sa.ForeignKey("inbound_events.id"),
            nullable=False,
        ),
        sa.Column("question_masked", sa.String(), nullable=False),
        sa.Column("refused", sa.Boolean(), nullable=False),
        sa.Column("refusal_reason", sa.String(length=_REASON_LENGTH), nullable=True),
        sa.Column("used_chunk_ids", sa.JSON(), nullable=True),
        sa.Column("model_name", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("kb_version", sa.String(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        # `server_default` обязателен: `CreatedAtMixin` не задаёт значение на
        # стороне Python и рассчитывает на `now()` от БД (как classification_logs).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "refusal_reason IN ("
            + ", ".join(f"'{reason}'" for reason in _REFUSAL_REASONS)
            + ")",
            name="ck_rights_logs_refusal_reason",
        ),
    )
    op.create_index("ix_rights_logs_inbound_event_id", "rights_logs", ["inbound_event_id"])


def downgrade() -> None:
    op.drop_index("ix_rights_logs_inbound_event_id", table_name="rights_logs")
    op.drop_table("rights_logs")
