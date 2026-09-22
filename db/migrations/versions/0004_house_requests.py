"""onboarding: house_requests for unconnected addresses

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22

ONBOARD-002: заявки жителей на подключение дома, которого нет в боте.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "house_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("address_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('new', 'connected', 'rejected')",
            name="ck_house_requests_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_house_requests_user_id", "house_requests", ["user_id"])
    op.create_index("ix_house_requests_status", "house_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_house_requests_status", table_name="house_requests")
    op.drop_index("ix_house_requests_user_id", table_name="house_requests")
    op.drop_table("house_requests")
