"""Загрузка предпосчитанной KB в Postgres + Qdrant (KB-001)."""

from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def _run() -> int:
    from upravdom.db import session_scope
    from upravdom.knowledge.loader import load_kb_from_artifacts
    from upravdom.vector_store import get_qdrant_client

    client = get_qdrant_client()
    async with session_scope() as session:
        stats = await load_kb_from_artifacts(session, client)
    print(f"[load_kb] OK {stats}", flush=True)
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception:  # noqa: BLE001
        logging.exception("load_kb failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
