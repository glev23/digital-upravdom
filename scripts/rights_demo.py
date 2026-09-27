"""Ручной прогон справки о правах против живой модели (RIGHTS-001).

Не вызывается из check.sh. Нужны Postgres, Qdrant с KB, OpenRouter в `.env`.

  docker compose up -d postgres qdrant
  uv run python scripts/load_kb.py
  uv run python scripts/rights_demo.py
  uv run python scripts/rights_demo.py "могут ли отключить горячую воду на месяц?"
  uv run python scripts/rights_demo.py --uk uk-vahitovskaya "за сколько приедет мастер?"

`--uk` подмешивает слой документов конкретной УК (KB-002): без него в поиске
только федеральные нормы, с ним — ещё и договор управления этой организации.

Вызовы тратят суточный лимит бесплатной модели — несколько прогонов, не циклы.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.config import get_settings
from upravdom.db import get_engine, session_scope
from upravdom.rights.service import answer_question

# Сочинённые вопросы — не из EVAL-001.
_DEMO_QUESTIONS: tuple[str, ...] = (
    "могут ли отключить горячую воду на месяц?",
    "за сколько должны предупредить о плановом отключении?",
    "сколько часов можно без холодной воды по нормативу?",
    "какой срок устранения аварии на внутридомовой системе?",
    "положен ли перерасчёт, если две недели холодные батареи?",
)


async def _inbound_event(session: AsyncSession) -> uuid.UUID:
    """Журнал ссылается на входящее событие — для демо заводим фиктивное."""

    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO inbound_events "
            "(id, max_event_id, payload, status, attempts, received_at) "
            "VALUES (:id, :m, '{}', 'done', 1, :ts)"
        ),
        {"id": event_id, "m": f"rights-demo-{event_id}", "ts": datetime.now(UTC)},
    )
    return event_id


def _company_id(raw: str | None) -> uuid.UUID | None:
    """UUID как есть либо ключ УК из демо-сида («uk-vahitovskaya»)."""

    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        # Как в classify_demo/run_accuracy_check: скрипт запускается из
        # каталога scripts, пакета `scripts` в sys.path нет.
        from seed_demo import stable_id

        return stable_id(f"mc:{raw}")


async def _run(questions: tuple[str, ...], company_id: uuid.UUID | None) -> int:
    settings = get_settings()
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY не задан — живой прогон невозможен", file=sys.stderr)
        return 1

    answered = 0
    for question in questions:
        async with session_scope() as session:
            event_id = await _inbound_event(session)
            answer = await answer_question(
                question,
                inbound_event_id=event_id,
                session=session,
                management_company_id=company_id,
                settings=settings,
            )
            await session.commit()

        print(f"\n❓ {question}")
        if answer.refused:
            reason = answer.refusal_reason.value if answer.refusal_reason else "—"
            print(f"   ОТКАЗ [{reason}], модель: {answer.model_name or '—'}")
            continue
        answered += 1
        print(f"   ОТВЕТ, модель: {answer.model_name}")
        print(f"   {answer.answer}")
        for label in answer.labels:
            print(f"   • {label}")

    print(f"\nитого: ответов {answered} из {len(questions)}")
    await get_engine().dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Живой прогон справки о правах")
    parser.add_argument("--uk", type=str, default=None, help="UUID или ключ УК из демо-сида")
    parser.add_argument("questions", nargs="*", help="вопросы; без них — набор по умолчанию")
    args = parser.parse_args()
    questions = tuple(args.questions) or _DEMO_QUESTIONS
    return asyncio.run(_run(questions, _company_id(args.uk)))


if __name__ == "__main__":
    sys.exit(main())
