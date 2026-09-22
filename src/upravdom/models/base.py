"""Базовый класс декларативных моделей и общие миксины (DB-001)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Общий `Base.metadata` — на него указывает `db/migrations/env.py`.

    `type_annotation_map`: любой `Mapped[datetime]` в моделях автоматически
    рендерится как `timestamptz`, а не голый `DateTime` (architecture.md §5 —
    "все метки времени — timestamptz"). Без этого пришлось бы явно указывать
    `DateTime(timezone=True)` в каждой колонке и рисковать пропустить одну.
    """

    type_annotation_map = {datetime: DateTime(timezone=True)}


class UUIDPKMixin:
    """Суррогатный первичный ключ UUID, генерируется на стороне Python."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class CreatedAtMixin:
    """`timestamptz` created_at (architecture.md §5 — "все метки времени — timestamptz")."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
