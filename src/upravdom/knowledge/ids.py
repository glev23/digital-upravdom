"""Детерминированные UUID документов и чанков базы знаний (KB-001)."""

from __future__ import annotations

import uuid

# Отдельное пространство от DATA-001 seed — ключи не пересекаются.
KB_NAMESPACE = uuid.UUID("b4e6d7e1-2345-4b22-af11-111111111111")


def document_id(source_key: str, version: str) -> uuid.UUID:
    return uuid.uuid5(KB_NAMESPACE, f"{source_key}:{version}")


def chunk_id(source_key: str, version: str, ref: str, part: int = 0) -> uuid.UUID:
    return uuid.uuid5(KB_NAMESPACE, f"{source_key}:{version}:{ref}:{part}")
