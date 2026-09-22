"""Пути и IO предпосчитанных артефактов `data/kb/` (KB-001)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from upravdom.config import get_settings
from upravdom.embeddings import PROMPT_SEARCH_DOCUMENT
from upravdom.knowledge.chunking import CHUNKER_VERSION, RawChunk
from upravdom.knowledge.ids import chunk_id, document_id

KB_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "kb"
SOURCES_DIR = KB_DIR / "sources"
CHUNKS_PATH = KB_DIR / "chunks.jsonl"
VECTORS_PATH = KB_DIR / "vectors.npy"
MANIFEST_PATH = KB_DIR / "manifest.json"
SANITY_QUERIES_PATH = KB_DIR / "sanity_queries.json"

KB_COLLECTION = "kb_chunks"
VECTOR_DIM = 768


@dataclass(slots=True, frozen=True)
class ArtifactChunk:
    chunk_id: str
    document_id: str
    chunk_text: str
    chunk_meta: dict[str, Any]
    document: dict[str, Any]


def compute_kb_version(sources_dir: Path = SOURCES_DIR) -> str:
    """Короткий sha256 от исходников + chunker + model + prompt документа."""

    h = hashlib.sha256()
    h.update(CHUNKER_VERSION.encode())
    h.update(get_settings().embedding_model_name.encode())
    h.update(PROMPT_SEARCH_DOCUMENT.encode())
    for path in sorted(sources_dir.glob("*.md")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def raw_to_artifact(raw: RawChunk) -> ArtifactChunk:
    doc = raw.document
    source_key = str(doc["source_key"])
    version = str(doc["version"])
    ref = str(raw.chunk_meta["ref"])
    part = int(raw.chunk_meta.get("part") or 0)
    return ArtifactChunk(
        chunk_id=str(chunk_id(source_key, version, ref, part)),
        document_id=str(document_id(source_key, version)),
        chunk_text=raw.chunk_text,
        chunk_meta=dict(raw.chunk_meta),
        document=dict(doc),
    )


def write_artifacts(
    chunks: list[ArtifactChunk],
    vectors: NDArray[np.float32],
    *,
    kb_version: str,
    built_at: str | None = None,
) -> None:
    if len(chunks) != vectors.shape[0]:
        msg = f"chunks={len(chunks)} != vectors={vectors.shape[0]}"
        raise ValueError(msg)
    if vectors.ndim != 2 or vectors.shape[1] != VECTOR_DIM:
        msg = f"ожидалась матрица N×{VECTOR_DIM}, получено {vectors.shape}"
        raise ValueError(msg)

    KB_DIR.mkdir(parents=True, exist_ok=True)
    with CHUNKS_PATH.open("w", encoding="utf-8") as fh:
        for ch in chunks:
            row = {
                "chunk_id": ch.chunk_id,
                "document_id": ch.document_id,
                "document": ch.document,
                "chunk_text": ch.chunk_text,
                "chunk_meta": ch.chunk_meta,
            }
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    np.save(VECTORS_PATH, vectors.astype(np.float32))
    manifest = {
        "kb_version": kb_version,
        "model_name": get_settings().embedding_model_name,
        "prompt_document": PROMPT_SEARCH_DOCUMENT,
        "dim": VECTOR_DIM,
        "chunker_version": CHUNKER_VERSION,
        "count": len(chunks),
        "built_at": built_at or datetime.now(UTC).isoformat(),
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def load_chunks(path: Path = CHUNKS_PATH) -> list[ArtifactChunk]:
    rows: list[ArtifactChunk] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows.append(
                ArtifactChunk(
                    chunk_id=row["chunk_id"],
                    document_id=row["document_id"],
                    chunk_text=row["chunk_text"],
                    chunk_meta=row["chunk_meta"],
                    document=row["document"],
                )
            )
    return rows


def load_vectors(path: Path = VECTORS_PATH) -> NDArray[np.float32]:
    arr = np.load(path)
    return np.asarray(arr, dtype=np.float32)
