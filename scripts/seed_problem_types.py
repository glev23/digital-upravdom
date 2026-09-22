"""Загрузка справочника problem_types из data/seed/problem_types.json (DB-001).

Идемпотентно (UPSERT по `code`) — безопасно запускать при каждом старте
контейнера, в отличие от миграции: обновление нормативного срока — это
правка JSON и повторный запуск, а не новая ревизия Alembic
(architecture.md §4 — "новый seed справочника, а не правка кода").
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import text

from upravdom.db import get_engine

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "seed" / "problem_types.json"

UPSERT_SQL = text(
    """
    INSERT INTO problem_types
        (code, title, default_responsibility_zone, resolution_hours, norm_reference)
    VALUES
        (:code, :title, :default_responsibility_zone, :resolution_hours, :norm_reference)
    ON CONFLICT (code) DO UPDATE SET
        title = EXCLUDED.title,
        default_responsibility_zone = EXCLUDED.default_responsibility_zone,
        resolution_hours = EXCLUDED.resolution_hours,
        norm_reference = EXCLUDED.norm_reference
    """
)


async def main() -> None:
    rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    engine = get_engine()
    async with engine.begin() as conn:
        for row in rows:
            await conn.execute(UPSERT_SQL, row)
    print(f"seeded {len(rows)} problem_types from {SEED_FILE.name}")


if __name__ == "__main__":
    asyncio.run(main())
