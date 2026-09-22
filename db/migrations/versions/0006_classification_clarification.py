"""FLOW-001: clarification JSON в classification_logs

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "classification_logs",
        sa.Column("clarification", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("classification_logs", "clarification")
