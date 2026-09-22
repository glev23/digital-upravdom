"""Сборка предпосчитанных артефактов data/kb/ (KB-001).

Запуск вручную при обновлении нормативов — не в критическом пути старта.
"""

from __future__ import annotations

import argparse
import sys

from upravdom.embeddings import PROMPT_SEARCH_DOCUMENT, embed
from upravdom.knowledge.artifacts import (
    CHUNKS_PATH,
    MANIFEST_PATH,
    SOURCES_DIR,
    VECTORS_PATH,
    compute_kb_version,
    raw_to_artifact,
    write_artifacts,
)
from upravdom.knowledge.chunking import chunk_sources_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка data/kb из sources/")
    parser.add_argument(
        "--sources",
        type=str,
        default=str(SOURCES_DIR),
        help="Каталог исходников Markdown",
    )
    args = parser.parse_args()
    from pathlib import Path

    sources = Path(args.sources)
    raw_chunks = chunk_sources_dir(sources)
    artifacts = [raw_to_artifact(c) for c in raw_chunks]
    # Стабильный порядок: по source_key, ref, part, chunk_id
    artifacts.sort(
        key=lambda c: (
            c.document["source_key"],
            str(c.chunk_meta.get("ref")),
            int(c.chunk_meta.get("part") or 0),
            c.chunk_id,
        )
    )
    texts = [c.chunk_text for c in artifacts]
    print(f"[build_kb] chunks={len(texts)}, embedding…", flush=True)
    # Батчами — ONNX на CPU, не держим всё в одном forward при сотнях чанков
    import numpy as np

    vectors = np.zeros((len(texts), 768), dtype=np.float32)
    batch = 16
    for i in range(0, len(texts), batch):
        part = texts[i : i + batch]
        vectors[i : i + len(part)] = embed(part, prompt=PROMPT_SEARCH_DOCUMENT)
        print(f"[build_kb] embedded {min(i + batch, len(texts))}/{len(texts)}", flush=True)

    kb_version = compute_kb_version(sources)
    write_artifacts(artifacts, vectors, kb_version=kb_version)
    print(
        f"[build_kb] OK version={kb_version} -> {CHUNKS_PATH.name}, "
        f"{VECTORS_PATH.name}, {MANIFEST_PATH.name}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
