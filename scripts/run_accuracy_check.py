"""Точность маршрутизации в четырёх режимах (EVAL-002, architecture.md §13.2).

Абсолютная цифра точности недоказательна: неизвестно, много это или мало.
Поэтому один и тот же набор прогоняется четырьмя способами, и в презентацию
идёт разрыв между ними, а не одно число.

| Режим      | Что моделирует                                                |
|------------|---------------------------------------------------------------|
| `menu`     | Форма с рубрикатором (ГИС ЖКХ / Госуслуги.Дом): житель сам     |
|            | выбирает категорию, а выбрать может только если сам назвал     |
|            | ресурс. Адресат — по умолчанию для категории, границу          |
|            | ответственности форма не считает                               |
| `keywords` | Бот на конструкторе без ИИ: словарь синонимов на категорию,    |
|            | адресат — тоже по умолчанию для категории                      |
| `product`  | Основной путь: маскирование → поиск по KB → LLM → порог        |
| `no-llm`   | Деградация §10: только прототипы, без внешней модели           |

Разница между `menu` и `keywords` — только богатство словаря: в `menu`
засчитывается лишь прямое название категории, в `keywords` — синонимы,
которые руками написал бы разработчик бота-конструктора. Обе формы не умеют
определять зону ответственности по смыслу и назначают её по типу проблемы —
в этом и состоит отличие продукта.

  docker compose up -d postgres qdrant
  uv run python scripts/run_accuracy_check.py --mode menu --mode keywords
  uv run python scripts/run_accuracy_check.py            # все четыре режима
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from seed_demo import seed, stable_id
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.classifier import Branch, classify
from upravdom.db import session_scope
from upravdom.models import ProblemType

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CASES = Path(__file__).resolve().parent.parent / "data" / "test_cases" / "accident_descriptions.json"
MODES = ("menu", "keywords", "product", "no-llm")

# Рубрикатор: только прямое название категории, как в форме на портале.
MENU_TERMS: dict[str, tuple[str, ...]] = {
    "cold_water": ("холодная вода", "холодной воды", "холодную воду", "хвс"),
    "hot_water": ("горячая вода", "горячей воды", "горячую воду", "гвс"),
    "heating": ("отопление", "отопления", "отоплением"),
    "sewage": ("канализация", "канализации", "канализацией", "канализационн"),
    "electricity": ("электричество", "электричества", "электроснабжение"),
    "gas": ("газ", "газа", "газом"),
    "elevator": ("лифт", "лифта", "лифты", "лифтом"),
    "roof_leak": ("кровля", "кровли", "крыша", "крыши", "крышу"),
    "common_area": ("подъезд", "подъезде", "подъезда", "места общего пользования"),
    "yard": ("двор", "дворе", "двора", "придомов"),
}

# Бот на конструкторе: словарь синонимов, написанный руками.
KEYWORD_TERMS: dict[str, tuple[str, ...]] = {
    "elevator": MENU_TERMS["elevator"] + ("кабина", "этаж", "лифтов"),
    "gas": MENU_TERMS["gas"] + ("плита", "конфорка", "газовщик"),
    "roof_leak": MENU_TERMS["roof_leak"] + ("потолок", "потолке", "чердак", "протека", "течет с"),
    "sewage": MENU_TERMS["sewage"] + ("стояк", "засор", "слив", "унитаз", "раковина", "стоки"),
    "heating": MENU_TERMS["heating"] + ("батаре", "радиатор", "холодно", "греет", "тепло"),
    "hot_water": MENU_TERMS["hot_water"] + ("полотенцесушитель", "горяч"),
    "cold_water": MENU_TERMS["cold_water"] + ("вода", "воды", "воду", "кран", "смеситель", "труба"),
    "electricity": MENU_TERMS["electricity"] + ("свет", "розетк", "щит", "проводк", "напряжен"),
    "common_area": MENU_TERMS["common_area"] + ("лестни", "домофон", "мусоропровод", "фундамент"),
    "yard": MENU_TERMS["yard"] + ("площадка", "асфальт", "тротуар", "контейнер"),
}


@dataclass(slots=True)
class Case:
    id: str
    text: str
    expected_type: str
    expected_zone: str
    difficulty: str
    source_kind: str


@dataclass(slots=True)
class Outcome:
    case: Case
    predicted_type: str
    predicted_zone: str
    abstained: bool
    # Решение до порога уверенности: показывает качество модели отдельно от
    # осторожности продукта. В основную таблицу не идёт.
    raw_type: str | None = None
    raw_zone: str | None = None


def load_cases() -> list[Case]:
    raw = json.loads(CASES.read_text(encoding="utf-8"))
    return [
        Case(
            id=r["id"],
            text=r["text"],
            expected_type=r["expected_problem_type"],
            expected_zone=r["expected_zone"],
            difficulty=r["difficulty"],
            source_kind=r["source_kind"],
        )
        for r in raw
    ]


def _match(text_: str, terms: dict[str, tuple[str, ...]]) -> str | None:
    """Первая категория, чей термин встретился; при ничьей — самое длинное совпадение."""

    lowered = " ".join(text_.lower().replace("ё", "е").split())
    best: tuple[int, str] | None = None
    for code, words in terms.items():
        for word in words:
            w = word.replace("ё", "е")
            if re.search(rf"\b{re.escape(w)}", lowered) and (best is None or len(w) > best[0]):
                best = (len(w), code)
    return best[1] if best else None


def run_dictionary_mode(
    cases: Sequence[Case], terms: dict[str, tuple[str, ...]], defaults: dict[str, str]
) -> list[Outcome]:
    """Форма/словарь: тип — по совпадению, зона — по умолчанию для типа.

    Ни рубрикатор, ни словарь не умеют читать границу ответственности из
    описания: «течёт после моего вентиля» и «течёт до вентиля» для них одно и
    то же слово «течёт». Поэтому зона берётся из справочника по типу — именно
    так работает форма с выбором категории.
    """

    out: list[Outcome] = []
    for case in cases:
        code = _match(case.text, terms)
        if code is None:
            out.append(Outcome(case, "other", "unknown", abstained=True))
        else:
            out.append(Outcome(case, code, defaults.get(code, "unknown"), abstained=False))
    return out


async def _no_cache(*_a: object, **_k: object) -> None:
    """Заглушка семантического кэша на время замера.

    Кэш проверяется раньше ветки `force_no_llm`, поэтому без заглушки режим
    `no-llm` переиспользовал бы решения, сохранённые прогоном `product`, и
    «деградация» показывала бы качество LLM. В рантайме кэш остаётся: там он
    экономит вызовы на повторах, а не подменяет один режим другим.
    """

    return None


async def run_classifier_mode(
    cases: Sequence[Case], session: AsyncSession, *, house_id: uuid.UUID, force_no_llm: bool
) -> list[Outcome]:
    out: list[Outcome] = []
    for case in cases:
        event_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO inbound_events "
                "(id, max_event_id, payload, status, attempts, received_at) "
                "VALUES (:id, :m, '{}', 'pending', 0, :ts)"
            ),
            {"id": event_id, "m": f"eval-{event_id}", "ts": datetime.now(UTC)},
        )
        result = await classify(
            case.text,
            house_id=house_id,
            inbound_event_id=event_id,
            session=session,
            force_no_llm=force_no_llm,
        )
        await session.commit()
        # Считаем то, что получает житель, а не внутреннее мнение модели: при
        # ветке не-`auto` бот честно отвечает «не берусь определить» и отдаёт
        # заявку диспетчеру с зоной unknown (FLOW-001). Если засчитывать
        # внутреннюю догадку, продукт сравнивался бы с рубрикатором по разным
        # правилам — тому отказ засчитывается промахом.
        abstained = result.branch is not Branch.AUTO
        out.append(
            Outcome(
                case,
                "other" if abstained else result.problem_type,
                "unknown" if abstained else result.responsibility_zone.value,
                abstained=abstained,
                raw_type=result.problem_type,
                raw_zone=result.responsibility_zone.value,
            )
        )
        print(f"    {case.id}: {result.problem_type}/{result.responsibility_zone.value}", flush=True)
    return out


def share(values: Iterable[bool]) -> tuple[int, int]:
    items = list(values)
    return sum(items), len(items)


def _fmt(hits: int, total: int) -> str:
    return f"{hits / total:.0%}" if total else "—"


def print_report(results: dict[str, list[Outcome]]) -> None:
    groups: list[tuple[str, object]] = [
        ("весь набор", lambda o: True),
        ("реальные", lambda o: o.case.source_kind != "synthetic"),
        ("сложные", lambda o: o.case.difficulty == "hard"),
        # Главная колонка: на наборе 70% случаев — зона УК, поэтому общая
        # точность по зоне почти не отличает продукт от «всегда УК».
        # Различают только случаи, где ответ не УК.
        ("зона не УК", lambda o: o.case.expected_zone != "uk"),
    ]
    print()
    header = f"{'режим':<10} {'ось':<22}" + "".join(f"{name:>18}" for name, _ in groups)
    print(header)
    print("-" * len(header))
    for mode, outcomes in results.items():
        for axis, ok in (
            ("тип проблемы", lambda o: o.predicted_type == o.case.expected_type),
            ("зона ответственности", lambda o: o.predicted_zone == o.case.expected_zone),
        ):
            row = f"{mode:<10} {axis:<22}"
            for _name, keep in groups:
                subset = [o for o in outcomes if keep(o)]  # type: ignore[operator]
                hits, total = share(ok(o) for o in subset)  # type: ignore[operator]
                row += f"{_fmt(hits, total) + f' {hits}/{total}':>18}"
            print(row)
        abst, total = share(o.abstained for o in outcomes)
        print(f"{'':<10} {'без ответа жителю':<22}{_fmt(abst, total) + f' {abst}/{total}':>18}")
        if any(o.raw_zone for o in outcomes):
            raw_hits, raw_total = share(o.raw_zone == o.case.expected_zone for o in outcomes)
            print(
                f"{'':<10} {'зона до порога':<22}{_fmt(raw_hits, raw_total) + f' {raw_hits}/{raw_total}':>18}"
            )
        print()
    baseline = [o for o in next(iter(results.values()))]
    always_uk, total = share(o.case.expected_zone == "uk" for o in baseline)
    print(f"Для сравнения: классификатор «всегда УК» дал бы {_fmt(always_uk, total)} по зоне.")


async def main_async(modes: Sequence[str], limit: int | None) -> int:
    from upravdom.classifier import service as classifier_service

    classifier_service.lookup_cache = _no_cache  # type: ignore[assignment]
    classifier_service.store_cache = _no_cache  # type: ignore[assignment]

    cases = load_cases()[:limit] if limit else load_cases()
    print(f"описаний: {len(cases)} (сложных: {sum(c.difficulty == 'hard' for c in cases)})")

    async with session_scope() as session:
        await seed(session)
        house_id = stable_id("house:house-dekabristov-10")
        rows = (
            await session.execute(
                select(ProblemType.code, ProblemType.default_responsibility_zone)
            )
        ).all()
        defaults = {r.code: r.default_responsibility_zone.value for r in rows}
        if not defaults:
            print("справочник problem_types пуст — нужен seed_problem_types.py", file=sys.stderr)
            return 1

        results: dict[str, list[Outcome]] = {}
        for mode in modes:
            print(f"\n=== {mode} ===", flush=True)
            if mode == "menu":
                results[mode] = run_dictionary_mode(cases, MENU_TERMS, defaults)
            elif mode == "keywords":
                results[mode] = run_dictionary_mode(cases, KEYWORD_TERMS, defaults)
            else:
                results[mode] = await run_classifier_mode(
                    cases, session, house_id=house_id, force_no_llm=(mode == "no-llm")
                )

    print_report(results)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", action="append", choices=MODES, dest="modes")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    return asyncio.run(main_async(args.modes or list(MODES), args.limit))


if __name__ == "__main__":
    sys.exit(main())
