"""Журнал классификации (architecture.md §5.6).

Доказательная база метрики точности, основание ответа жителю и
диагностика деградаций — одна таблица закрывает все три задачи.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from upravdom.models.base import Base, CreatedAtMixin, UUIDPKMixin
from upravdom.models.enums import ResponsibilityZone, pg_enum


class ClassificationLog(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "classification_logs"

    inbound_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inbound_events.id"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    # Только маскированный текст — architecture.md §5.6, §11.
    message_masked: Mapped[str] = mapped_column(String, nullable=False)
    problem_type: Mapped[str] = mapped_column(ForeignKey("problem_types.code"), nullable=False)
    responsibility_zone: Mapped[ResponsibilityZone] = mapped_column(
        pg_enum(ResponsibilityZone, name="ck_classification_logs_responsibility_zone"),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    used_chunk_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Деградация без LLM не имеет ни модели, ни версии промпта — nullable.
    model_name: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    kb_version: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # FLOW-001: вопрос и варианты первого прохода (ветка clarify).
    clarification: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
