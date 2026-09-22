"""SPIKE-001: замер задержки эмбеддинга и совпадения векторов A vs B."""

from __future__ import annotations

import json
import time
from pathlib import Path

PHRASES = [
    "второй день из стены за ванной сифонит вода",
    "нет света в подъезде на третьем этаже",
    "течёт с потолка после дождя",
    "батареи чуть тёплые второй день подряд",
    "лифт не работает уже неделю",
    "во дворе не убирают снег",
    "горячей воды нет с утра",
    "в квартире холодно, отопление слабое",
    "канализация засорилась в подвале",
    "разбито стекло в подъезде",
] * 5  # 50 фраз


def measure(model_name: str, backend: str) -> None:
    from sentence_transformers import SentenceTransformer

    t0 = time.perf_counter()
    model = SentenceTransformer(model_name, backend=backend, device="cpu")
    load_s = time.perf_counter() - t0

    # прогрев
    model.encode(["прогрев"])

    t0 = time.perf_counter()
    vectors = model.encode(PHRASES)
    total_s = time.perf_counter() - t0
    per_item_ms = (total_s / len(PHRASES)) * 1000

    out_dir = Path(__file__).resolve().parent.parent / "var" / "spike"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"vectors_{backend}.json"
    out.write_text(json.dumps(vectors.tolist()))

    print(
        json.dumps(
            {
                "backend": backend,
                "load_s": round(load_s, 2),
                "per_item_ms": round(per_item_ms, 1),
                "dim": vectors.shape[1],
            }
        )
    )


if __name__ == "__main__":
    import sys

    backend = sys.argv[1] if len(sys.argv) > 1 else "torch"
    measure("sergeyzh/BERTA", backend)
