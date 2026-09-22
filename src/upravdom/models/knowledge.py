"""База знаний: версионируемые документы и чанки (architecture.md §5.5)."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from upravdom.models.base import Base, CreatedAtMixin, UUIDPKMixin
from upravdom.models.enums import IndexState, pg_enum


class KnowledgeDocument(UUIDPKMixin, CreatedAtMixin, Base):
    """Неизменяемы и версионны: новая редакция — новая строка, не UPDATE."""

    __tablename__ = "knowledge_documents"

    source_key: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    effective_from: Mapped[date] = mapped_column(nullable=False)
    effective_to: Mapped[date | None] = mapped_column(nullable=True)
    region_code: Mapped[str | None] = mapped_column(String, nullable=True)
    management_company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("management_companies.id"), nullable=True
    )
    checksum: Mapped[str] = mapped_column(String, nullable=False)


class KnowledgeChunk(UUIDPKMixin, CreatedAtMixin, Base):
    """`chunk_meta`, не `metadata` — конфликтует с `Base.metadata` (§5.5)."""

    __tablename__ = "knowledge_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id"), nullable=False
    )
    chunk_text: Mapped[str] = mapped_column(String, nullable=False)
    chunk_meta: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    qdrant_point_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    index_state: Mapped[IndexState] = mapped_column(
        pg_enum(IndexState, name="ck_knowledge_chunks_index_state"),
        nullable=False,
        default=IndexState.PENDING,
    )
