"""Ручной прогон classify() против живой модели (CLASSIFY-001).

Не вызывается из check.sh. Нужны Postgres, Qdrant с KB, OpenRouter в .env.

  docker compose up -d postgres qdrant
  uv run python scripts/load_kb.py
  uv run python scripts/seed_problem_types.py
  uv run python scripts/seed_demo.py
  uv run python scripts/classify_demo.py
  uv run python scripts/classify_demo.py "из стены за ванной сифонит, плинтус повело"
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime

from seed_demo import seed, stable_id
from sqlalchemy import text

from upravdom.classifier import Branch, classify
from upravdom.classifier.llm import OpenRouterClient
from upravdom.db import session_scope

# Сочинённые фразы — не из EVAL-001.
_DEMO_PHRASES: tuple[str, ...] = (
    "из стены за ванной сифонит, плинтус повело",
    "второй день из крана на кухне не идёт холодная вода",
    "в подъезде на площадке третьего этажа разбито окно",
    "лифт застрял между вторым и третьим, двери не открываются",
    "с потолка в комнате капает после дождя — похоже кровля",
    "батарея в зале чуть тёплее ладони, на улице минус",
    "воняет канализацией из стояка в туалете",
    "во дворе у детской площадки яма и сломанная урна",
    "после вентиля под мойкой течёт только у меня в квартире",
    "пропал свет во всей квартире, у соседей горит",
    "нет горячей воды уже сутки, холодная есть",
    "на лестничной клетке мигает лампочка и пахнет гарью",
    "газ в плите не зажигается, запаха в подъезде нет",
    "лужа у мусорных баков на придомовой — кто чинит?",
    "непонятный гул в стене по ночам, не знаю к кому",
)


async def _ensure_inbound(session: object) -> uuid.UUID:
    event_id = uuid.uuid4()
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO inbound_events "
            "(id, max_event_id, payload, status, attempts, received_at) "
            "VALUES (:id, :m, '{}', 'pending', 0, :ts)"
        ),
        {
            "id": event_id,
            "m": f"demo-{event_id}",
            "ts": datetime.now(UTC),
        },
    )
    return event_id


async def _run(phrases: list[str]) -> int:
    llm = OpenRouterClient()
    counts = {Branch.AUTO: 0, Branch.CLARIFY: 0, Branch.UNKNOWN: 0}
    async with session_scope() as session:
        await seed(session)
        house_id = stable_id("house:house-dekabristov-10")
        for phrase in phrases:
            event_id = await _ensure_inbound(session)
            result = await classify(
                phrase,
                house_id=house_id,
                inbound_event_id=event_id,
                session=session,
                llm=llm,
            )
            await session.commit()
            counts[result.branch] = counts.get(result.branch, 0) + 1
            refs = ", ".join(c.ref for c in result.citations) or "—"
            print(
                f"[{result.branch.value}] {result.problem_type}/"
                f"{result.responsibility_zone.value} "
                f"conf={result.confidence:.2f} model={result.model_name} "
                f"fallback={result.fallback_used} refs={refs}",
                flush=True,
            )
            print(f"  «{phrase}»", flush=True)
            if result.clarifying_question:
                print(
                    f"  Q: {result.clarifying_question} | {result.clarifying_options}",
                    flush=True,
                )

    print(
        f"\nветви: auto={counts[Branch.AUTO]} "
        f"clarify={counts[Branch.CLARIFY]} unknown={counts[Branch.UNKNOWN]}",
        flush=True,
    )
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        phrases = [" ".join(sys.argv[1:])]
    else:
        phrases = list(_DEMO_PHRASES)
    return asyncio.run(_run(phrases))


if __name__ == "__main__":
    sys.exit(main())
