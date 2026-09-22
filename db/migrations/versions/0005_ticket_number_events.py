"""TICKET-001: короткий номер заявки, типы событий, идемпотентность source_event.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE ticket_number_seq AS BIGINT")
    op.add_column(
        "tickets",
        sa.Column(
            "number",
            sa.BigInteger(),
            nullable=True,
            server_default=sa.text("nextval('ticket_number_seq')"),
        ),
    )
    op.execute("UPDATE tickets SET number = nextval('ticket_number_seq') WHERE number IS NULL")
    op.alter_column("tickets", "number", nullable=False)
    op.create_unique_constraint("uq_tickets_number", "tickets", ["number"])
    op.execute("ALTER SEQUENCE ticket_number_seq OWNED BY tickets.number")

    op.create_index(
        "uq_tickets_source_event_id",
        "tickets",
        ["source_event_id"],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NOT NULL"),
    )

    op.create_check_constraint(
        "ck_ticket_events_event_type",
        "ticket_events",
        "event_type IN ('created', 'status_changed', 'routed', 'subscriber_joined')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ticket_events_event_type", "ticket_events", type_="check")
    op.drop_index("uq_tickets_source_event_id", table_name="tickets")
    op.drop_constraint("uq_tickets_number", "tickets", type_="unique")
    op.drop_column("tickets", "number")
    op.execute("DROP SEQUENCE IF EXISTS ticket_number_seq")
