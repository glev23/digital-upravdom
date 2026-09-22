"""Сборка векторов прототипов data/classifier/ (CLASSIFY-002).

uv run python scripts/build_prototypes.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from upravdom.config import get_settings
from upravdom.embeddings import PROMPT_CLASSIFICATION, embed

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "data" / "classifier"
JSON_PATH = DIR / "prototypes.json"
NPY_PATH = DIR / "prototypes.npy"
MANIFEST_PATH = DIR / "manifest.json"


def main() -> int:
    rows = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    texts = [str(r["text"]) for r in rows]
    print(f"[build_prototypes] n={len(texts)}, embedding…", flush=True)
    vectors = np.zeros((len(texts), 768), dtype=np.float32)
    batch = 16
    for i in range(0, len(texts), batch):
        part = texts[i : i + batch]
        vectors[i : i + len(part)] = embed(part, prompt=PROMPT_CLASSIFICATION)
        print(f"[build_prototypes] {min(i + batch, len(texts))}/{len(texts)}", flush=True)
    NPY_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.save(NPY_PATH, vectors)
    manifest = {
        "count": len(texts),
        "dim": 768,
        "prompt": PROMPT_CLASSIFICATION,
        "model_name": get_settings().embedding_model_name,
        "built_at": datetime.now(UTC).isoformat(),
        "source": "prototypes.json",
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"[build_prototypes] OK -> {NPY_PATH.name}, {MANIFEST_PATH.name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
