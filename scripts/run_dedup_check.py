"""Калибровка порога склейки и доля ложных склеек (DEDUP-001, architecture.md §13.2).

Проверка идёт на уровне функции сравнения (эмбеддинг + порог), без LLM и без
БД: измеряется именно решение «то же самое или нет».

  uv run python scripts/run_dedup_check.py
  uv run python scripts/run_dedup_check.py --threshold 0.90

Печатаются обе цифры, и **доля ложных склеек показывается всегда**, даже если
она хуже доли верных: пропущенный дубль стоит диспетчеру лишней строки, а
ложная склейка означает, что проблему жителя потеряли (§6.5).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from upravdom.config import get_settings
from upravdom.dedup.similarity import cosine, embed_many_for_dedup
from upravdom.masking import mask

CASES = Path(__file__).resolve().parent.parent / "data" / "test_cases" / "dedup_cases.json"

# Диапазон подбора расширен вниз против architecture.md §6.5: на BERTA косинус
# между разными формулировками одной аварии держится в районе 0.3–0.8, и на
# 0.80–0.95 не склеивается вообще ничего (прогон 22.09.2026, см. dedup-001.md).
_SWEEP_FROM = 0.50
_SWEEP_TO = 0.90
_SWEEP_STEP = 0.02


@dataclass(slots=True, frozen=True)
class Scores:
    """Косинусы по трём выборкам.

    `reachable` — пары «разные проблемы», которые в рантайме вообще доходят до
    сравнения векторов: у них совпадает `problem_type` (и дом, и окно — в
    продакшне). Это и есть настоящий риск ложной склейки. `blocked` — пары,
    которые отсекает фильтр по типу ещё до косинуса; они показываются
    отдельно, чтобы не завышать качество порога.
    """

    same: list[float]
    #: Почти дословные повторы соседей — как в настоящей массовой аварии.
    #: Группы `same` намеренно написаны максимально по-разному, и по ним одним
    #: порог выбрать нельзя: они меряют верхнюю границу задачи, а не рабочий случай.
    near: list[float]
    reachable: list[float]
    blocked: list[float]

    @property
    def different(self) -> list[float]:
        return [*self.reachable, *self.blocked]


def _vectors(texts: list[str]) -> list[list[float]]:
    # Тот же вход, что в рантайме: эмбеддинг считается по маскированному тексту.
    return embed_many_for_dedup([str(mask(t).text) for t in texts])


def collect(payload: dict[str, object]) -> Scores:
    groups = payload.get("groups") or []
    pairs = payload.get("control_pairs") or []
    assert isinstance(groups, list) and isinstance(pairs, list)

    same: list[float] = []
    for group in groups:
        texts = list(group["texts"])
        vectors = _vectors(texts)
        same.extend(cosine(a, b) for a, b in combinations(vectors, 2))

    near: list[float] = []
    for pair in payload.get("near_duplicates") or []:
        left, right = _vectors([pair["left"], pair["right"]])
        near.append(cosine(left, right))

    reachable: list[float] = []
    blocked: list[float] = []
    for pair in pairs:
        left, right = _vectors([pair["left"], pair["right"]])
        score = cosine(left, right)
        if pair.get("left_problem_type") == pair.get("right_problem_type"):
            reachable.append(score)
        else:
            blocked.append(score)

    # Пары первых фраз из разных групп — тоже «разные аварии»: контрольных пар
    # мало, а перекрёстные сравнения ловят склейку «всё похоже на всё».
    heads = [(str(g["problem_type"]), list(g["texts"])[0]) for g in groups]
    head_vectors = _vectors([text for _type, text in heads])
    for left_i, right_i in combinations(range(len(heads)), 2):
        score = cosine(head_vectors[left_i], head_vectors[right_i])
        if heads[left_i][0] == heads[right_i][0]:
            reachable.append(score)
        else:
            blocked.append(score)
    return Scores(same=same, near=near, reachable=reachable, blocked=blocked)


def rates(scores: Scores, threshold: float) -> tuple[float, float, float]:
    """(верные склейки на группах, верные на почти дословных, ЛОЖНЫЕ склейки)."""

    def share(values: list[float]) -> float:
        return sum(1 for s in values if s >= threshold) / len(values) if values else 0.0

    return share(scores.same), share(scores.near), share(scores.reachable)


def main() -> int:
    parser = argparse.ArgumentParser(description="Доли верных и ложных склеек")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="порог; по умолчанию DEDUP_SIMILARITY_THRESHOLD из окружения",
    )
    args = parser.parse_args()

    if not CASES.is_file():
        print(f"нет набора: {CASES}", file=sys.stderr)
        return 1
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    scores = collect(payload)
    current = (
        args.threshold if args.threshold is not None else get_settings().dedup_similarity_threshold
    )

    print(
        f"пар «одна авария, разные формулировки»: {len(scores.same)}; "
        f"«почти дословные повторы»: {len(scores.near)}; "
        f"«разные аварии, тот же problem_type» (доходят до косинуса): {len(scores.reachable)}; "
        f"отсечено фильтром типа до сравнения: {len(scores.blocked)}"
    )
    print()
    print("порог   разные формулировки   почти дословные   ЛОЖНЫЕ склейки")
    step = int(round((_SWEEP_TO - _SWEEP_FROM) / _SWEEP_STEP))
    for i in range(step + 1):
        threshold = round(_SWEEP_FROM + i * _SWEEP_STEP, 2)
        loose, near, false = rates(scores, threshold)
        marker = " ←" if abs(threshold - current) < 1e-9 else ""
        print(f"{threshold:5.2f}   {loose:>18.0%}   {near:>15.0%}   {false:>14.0%}{marker}")

    loose, near, false = rates(scores, current)
    print()
    print(
        f"на текущем пороге {current:.2f}: разные формулировки {loose:.0%}, "
        f"почти дословные {near:.0%}, ЛОЖНЫХ {false:.0%}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
