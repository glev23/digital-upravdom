"""Smoke-проверка поиска по базе знаний на контрольных запросах (KB-001).

Не метрика продукта (она — EVAL-002): проверяет, что для типовых вопросов
жителя нужный пункт норматива оказывается в top-k. Требует загруженной
коллекции `kb_chunks` (`python scripts/load_kb.py`).

    uv run python scripts/kb_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from upravdom.knowledge.artifacts import SANITY_QUERIES_PATH
from upravdom.knowledge.retrieval import search

TOP_K = 5
THRESHOLD = 0.8


async def main() -> int:
    queries = json.loads(SANITY_QUERIES_PATH.read_text(encoding="utf-8"))
    hits = 0
    for q in queries:
        found = await search(q["query"], k=TOP_K)
        ok = any(r.source_key == q["source_key"] and q["ref_contains"] in r.ref for r in found)
        hits += ok
        top = ", ".join(f"{r.source_key}:{r.ref}" for r in found[:3])
        print(
            f"{'OK  ' if ok else 'MISS'} {q['query']}  [ждём {q['source_key']} {q['ref_contains']}; top3: {top}]"
        )
    rate = hits / len(queries)
    print(f"\nпопаданий в top-{TOP_K}: {hits}/{len(queries)} ({rate:.0%}), порог {THRESHOLD:.0%}")
    return 0 if rate >= THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
