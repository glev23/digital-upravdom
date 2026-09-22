"""onboarding: hold inbound events until consent

Revision ID: 0003
Revises: 0002
Original create date: 2026-09-22

Новое значение `awaiting_consent` в CHECK статуса и `max_user_id` с индексом
(ONBOARD-001, architecture.md §7.1 — удержание сообщения до согласия на ПДн).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = ("pending", "processing", "done", "failed")
_NEW = (*_OLD, "awaiting_consent")


def _check(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_inbound_events_status", "inbound_events", type_="check")
    # VARCHAR + CHECK по enum получает длину самого длинного значения
    # (`processing` → VARCHAR(10)); `awaiting_consent` в неё не помещается.
    op.alter_column(
        "inbound_events",
        "status",
        existing_type=sa.String(length=10),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.create_check_constraint("ck_inbound_events_status", "inbound_events", _check(_NEW))
    op.add_column("inbound_events", sa.Column("max_user_id", sa.String(), nullable=True))
    op.create_index("ix_inbound_events_max_user_id", "inbound_events", ["max_user_id"])


def downgrade() -> None:
    op.drop_index("ix_inbound_events_max_user_id", table_name="inbound_events")
    op.drop_column("inbound_events", "max_user_id")
    # Удержанные события иначе нарушили бы старый CHECK — закрываем их как
    # необработанные, а не теряем молча.
    op.execute(
        "UPDATE inbound_events SET status = 'done', last_error = 'downgrade: held event closed' "
        "WHERE status = 'awaiting_consent'"
    )
    op.drop_constraint("ck_inbound_events_status", "inbound_events", type_="check")
    op.alter_column(
        "inbound_events",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    op.create_check_constraint("ck_inbound_events_status", "inbound_events", _check(_OLD))
