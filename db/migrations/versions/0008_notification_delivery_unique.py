"""NOTIFY-001: одно уведомление одному жителю — не больше одного раза.

Уникальность на уровне БД, а не проверка «есть ли уже запись» перед
вставкой: между проверкой и вставкой помещается второй проход задачи или
перезапуск приложения — тот же приём, что у inbox в BOT-001.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "uq_notification_deliveries_notification_user"


def upgrade() -> None:
    # Дубли до введения ограничения возможны только в дев-базе; оставляем
    # самую раннюю доставку, иначе уникальный индекс не создастся.
    op.execute(
        """
        DELETE FROM notification_deliveries a
        USING notification_deliveries b
        WHERE a.notification_id = b.notification_id
          AND a.user_id = b.user_id
          AND a.created_at > b.created_at
        """
    )
    op.create_unique_constraint(
        _CONSTRAINT,
        "notification_deliveries",
        ["notification_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "notification_deliveries", type_="unique")
