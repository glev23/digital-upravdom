"""DEDUP-001: вектор обращения на заявке и тип события subscriber_left.

Вектор хранится в Postgres, а не в Qdrant: кандидаты ищутся среди заявок
одного дома (выборка мала), и недоступность Qdrant не должна ломать
дедупликацию — architecture.md §6.5, §9.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_TYPES_BEFORE = "('created', 'status_changed', 'routed', 'subscriber_joined')"
_EVENT_TYPES_AFTER = (
    "('created', 'status_changed', 'routed', 'subscriber_joined', 'subscriber_left')"
)


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("text_embedding", postgresql.ARRAY(sa.REAL()), nullable=True),
    )
    # Изменение CHECK — drop + create с тем же именем (db-001.md).
    op.drop_constraint("ck_ticket_events_event_type", "ticket_events", type_="check")
    op.create_check_constraint(
        "ck_ticket_events_event_type",
        "ticket_events",
        f"event_type IN {_EVENT_TYPES_AFTER}",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ticket_events_event_type", "ticket_events", type_="check")
    op.create_check_constraint(
        "ck_ticket_events_event_type",
        "ticket_events",
        f"event_type IN {_EVENT_TYPES_BEFORE}",
    )
    op.drop_column("tickets", "text_embedding")
