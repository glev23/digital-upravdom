"""Сравнение бесплатных моделей OpenRouter на реальном промпте (CLASSIFY-003).

Меряет **модель**, а не пайплайн: промпт собирается как в рабочем пути
(реальный поиск по KB + справочник problem_types + маскирование), но
семантический кэш и журнал не участвуют. Это принципиально: кэш вернул бы
на повторе сохранённое решение, и нестабильность модели — главная причина
дыры CLASSIFY-003 — осталась бы невидимой.

Резервная модель на время прогона отключается, иначе сбой кандидата молча
подменялся бы ответом другой модели и портил сравнение.

  docker compose up -d postgres qdrant
  uv run python scripts/model_bakeoff.py --phrases 3
  uv run python scripts/model_bakeoff.py --models qwen/qwen3.8-27b:free --repeats 5
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import partial

from classify_demo import _DEMO_PHRASES
from sqlalchemy import select

from upravdom.classifier.llm import LlmError, OpenRouterClient
from upravdom.classifier.llm.port import LlmRateLimited
from upravdom.classifier.llm.port import ChatMessage
from upravdom.classifier.prompt import ProblemTypeInfo, build_messages
from upravdom.classifier.schema import LlmClassification
from upravdom.classifier.service import confirm_citations
from upravdom.config import get_settings
from upravdom.db import session_scope
from upravdom.embeddings import PROMPT_SEARCH_QUERY, embed
from upravdom.knowledge.retrieval import RetrievedChunk, search
from upravdom.masking import mask
from upravdom.models import ProblemType

# Консоль Windows по умолчанию cp1251 и падает на «×» и кириллице в выводе.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Бесплатные модели известных вендоров, поддерживающие строгий JSON-schema
# (сверено с GET /api/v1/models: supported_parameters). Текущая рабочая
# nex-n2.5-mini оставлена последней как база для сравнения.
DEFAULT_CANDIDATES: tuple[str, ...] = (
    "qwen/qwen3.8-27b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "nex-agi/nex-n2.5-pro:free",
    "nex-agi/nex-n2.5-mini:free",
)


@dataclass(slots=True)
class Outcome:
    phrase_idx: int
    ok: bool
    zone: str | None = None
    problem_type: str | None = None
    confidence: float = 0.0
    cited_confirmed: int = 0
    cited_raw: int = 0
    latency_ms: int = 0
    error: str | None = None


@dataclass(slots=True)
class ModelReport:
    model: str
    outcomes: list[Outcome] = field(default_factory=list)
    skipped: str | None = None

    @property
    def ok_calls(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.ok]

    def auto_share(self, high: float) -> float:
        """Доля решений, которые прошли бы в auto по реальному правилу.

        Правило CLASSIFY-001: без подтверждённой ссылки на норму уверенность
        срезается ниже порога, то есть auto невозможен.
        """

        ok = self.ok_calls
        if not ok:
            return 0.0
        passed = sum(1 for o in ok if o.cited_confirmed > 0 and o.confidence >= high)
        return passed / len(ok)

    def cited_share(self) -> float:
        ok = self.ok_calls
        if not ok:
            return 0.0
        return sum(1 for o in ok if o.cited_confirmed > 0) / len(ok)

    def stability(self, repeats: int) -> float | None:
        """Доля фраз, где модальная зона занимает ≥80% повторов."""

        if repeats < 2:
            return None
        by_phrase: dict[int, list[str]] = {}
        for o in self.ok_calls:
            if o.zone:
                by_phrase.setdefault(o.phrase_idx, []).append(o.zone)
        measured = [zones for zones in by_phrase.values() if len(zones) >= 2]
        if not measured:
            return 0.0
        stable = 0
        for zones in measured:
            top = Counter(zones).most_common(1)[0][1]
            if top / len(zones) >= 0.8:
                stable += 1
        return stable / len(measured)

    def latency_p50(self) -> int:
        ok = self.ok_calls
        if not ok:
            return 0
        return int(statistics.median(o.latency_ms for o in ok))

    def errors(self) -> str:
        bad = [o.error for o in self.outcomes if not o.ok and o.error]
        if not bad:
            return "—"
        top = Counter(bad).most_common(2)
        return "; ".join(f"{msg} x{n}" for msg, n in top)


async def _load_problem_types() -> list[ProblemTypeInfo]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    ProblemType.code,
                    ProblemType.title,
                    ProblemType.default_responsibility_zone,
                    ProblemType.norm_reference,
                ).order_by(ProblemType.code)
            )
        ).all()
    return [
        ProblemTypeInfo(
            code=r.code,
            title=r.title,
            default_responsibility_zone=r.default_responsibility_zone,
            norm_reference=r.norm_reference,
        )
        for r in rows
    ]


async def _build_prompts(
    phrases: list[str], problem_types: list[ProblemTypeInfo]
) -> list[tuple[str, list[ChatMessage], list[RetrievedChunk]]]:
    """Поиск по KB делается один раз на фразу и переиспользуется всеми моделями.

    Иначе кандидаты сравнивались бы на разных контекстах.
    """

    out: list[tuple[str, list[ChatMessage], list[RetrievedChunk]]] = []
    mask_ms: list[float] = []
    embed_ms: list[float] = []
    search_ms: list[float] = []
    for phrase in phrases:
        t0 = time.perf_counter()
        masked = mask(phrase).text
        t1 = time.perf_counter()
        # embed отдельно от поиска: внутри search() они слиты, а для критерия
        # приёмки задержку надо разложить по этапам.
        await asyncio.to_thread(partial(embed, [str(masked)], prompt=PROMPT_SEARCH_QUERY))
        t2 = time.perf_counter()
        chunks = await search(str(masked), k=5)
        t3 = time.perf_counter()
        mask_ms.append((t1 - t0) * 1000)
        embed_ms.append((t2 - t1) * 1000)
        search_ms.append((t3 - t2) * 1000)
        messages = build_messages(masked, chunks=chunks, problem_types=problem_types)
        out.append((phrase, messages, chunks))

    print(
        "этапы до LLM (медиана): "
        f"маскирование {statistics.median(mask_ms):.0f} мс, "
        f"эмбеддинг {statistics.median(embed_ms):.0f} мс, "
        f"поиск+эмбеддинг {statistics.median(search_ms):.0f} мс",
        flush=True,
    )
    return out


async def _call_with_throttle_retry(
    client: OpenRouterClient,
    messages: list[ChatMessage],
    *,
    retries: int,
    backoff_s: float,
):  # type: ignore[no-untyped-def]
    """429 — это лимит бесплатного тарифа, а не свойство модели.

    Без повторов замер превращался бы в измерение очереди: модель, которой
    не досталось слота, выглядела бы неработающей.
    """

    last: LlmError | None = None
    for attempt in range(retries + 1):
        try:
            return await client.complete_json(messages, schema=LlmClassification)
        except LlmRateLimited as exc:
            last = exc
            if attempt < retries:
                await asyncio.sleep(backoff_s * (attempt + 1))
                continue
            raise
    assert last is not None
    raise last


async def probe_model(
    model: str,
    prompts: list[tuple[str, list[ChatMessage], list[RetrievedChunk]]],
    *,
    repeats: int,
    timeout_s: float,
    pace_s: float,
    verbose: bool,
    rate_limit_retries: int = 3,
    rate_limit_backoff_s: float = 20.0,
) -> ModelReport:
    base = get_settings()
    settings = base.model_copy(
        update={
            "openrouter_model": model,
            # Резерв отключён: иначе сбой кандидата подменится чужим ответом.
            "openrouter_model_fallback": None,
            # Лимит и размыкатель — защита рантайма, в замере они только
            # обрезали бы выборку; ранний выход сделан явным ниже.
            "llm_max_requests_per_minute": 100_000,
            "llm_circuit_failure_threshold": 100_000,
            "llm_timeout_seconds": timeout_s,
        }
    )
    client = OpenRouterClient(settings=settings)
    report = ModelReport(model=model)

    for attempt in range(repeats):
        for idx, (phrase, messages, chunks) in enumerate(prompts):
            started = time.perf_counter()
            try:
                result = await _call_with_throttle_retry(
                    client, messages, retries=rate_limit_retries, backoff_s=rate_limit_backoff_s
                )
            except LlmError as exc:
                report.outcomes.append(
                    Outcome(
                        phrase_idx=idx,
                        ok=False,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        error=f"{type(exc).__name__}: {exc}"[:80],
                    )
                )
                # Модель, которая падает на первых двух вызовах, дальше не
                # проверяется — бесплатные лимиты тратить на неё незачем.
                # Троттлинг сюда не попадает: он уже пережит повторами выше и
                # означает лимит тарифа, а не непригодность модели.
                if len(report.outcomes) == 2 and not report.ok_calls:
                    report.skipped = report.outcomes[0].error
                    return report
                continue

            data = result.data
            assert isinstance(data, LlmClassification)
            confirmed = confirm_citations(data.cited_fragments, chunks)
            outcome = Outcome(
                phrase_idx=idx,
                ok=True,
                zone=data.responsibility_zone.value,
                problem_type=data.problem_type,
                confidence=float(data.confidence),
                cited_confirmed=len(confirmed),
                cited_raw=len(data.cited_fragments),
                latency_ms=result.latency_ms,
            )
            report.outcomes.append(outcome)
            if verbose:
                print(
                    f"    [{attempt + 1}/{repeats}] «{phrase[:40]}…» → "
                    f"{outcome.problem_type}/{outcome.zone} "
                    f"conf={outcome.confidence:.2f} "
                    f"frag={outcome.cited_confirmed}/{outcome.cited_raw} "
                    f"{outcome.latency_ms} мс",
                    flush=True,
                )
            if pace_s:
                await asyncio.sleep(pace_s)
    return report


def print_table(reports: list[ModelReport], *, high: float, repeats: int) -> None:
    print()
    print(
        f"{'модель':<44} {'ok':>7} {'ссылки':>7} {'auto':>6} "
        f"{'стаб.':>6} {'p50 мс':>7}  ошибки"
    )
    print("-" * 110)
    for rep in reports:
        if rep.skipped:
            print(f"{rep.model:<44} {'—':>7} {'—':>7} {'—':>6} {'—':>6} {'—':>7}  {rep.skipped}")
            continue
        total = len(rep.outcomes)
        ok = len(rep.ok_calls)
        stab = rep.stability(repeats)
        stab_s = f"{stab:.0%}" if stab is not None else "—"
        print(
            f"{rep.model:<44} {ok:>3}/{total:<3} {rep.cited_share():>6.0%} "
            f"{rep.auto_share(high):>6.0%} {stab_s:>6} {rep.latency_p50():>7}  {rep.errors()}"
        )
    print()
    print(f"auto — доля, прошедшая бы порог {high} при подтверждённой ссылке на норму")
    if repeats >= 2:
        print(f"стаб. — доля фраз с одинаковой зоной в ≥80% из {repeats} повторов")


async def _run(args: argparse.Namespace) -> int:
    problem_types = await _load_problem_types()
    if not problem_types:
        print("справочник problem_types пуст — нужен seed_problem_types.py", file=sys.stderr)
        return 1

    phrases = list(_DEMO_PHRASES)[: args.phrases]
    print(f"фраз: {len(phrases)}, повторов: {args.repeats}, моделей: {len(args.models)}")
    print("сбор контекста из базы знаний…", flush=True)
    prompts = await _build_prompts(phrases, problem_types)
    empty = sum(1 for _, _, chunks in prompts if not chunks)
    if empty:
        print(f"ВНИМАНИЕ: у {empty} фраз поиск не дал фрагментов — KB загружена?", file=sys.stderr)

    high = get_settings().classify_confidence_high
    reports: list[ModelReport] = []
    for model in args.models:
        print(f"\n=== {model} ===", flush=True)
        report = await probe_model(
            model,
            prompts,
            repeats=args.repeats,
            timeout_s=args.timeout,
            pace_s=args.pace,
            verbose=args.verbose,
        )
        reports.append(report)
        if report.skipped:
            print(f"    пропущена: {report.skipped}", flush=True)
        else:
            print(
                f"    ok={len(report.ok_calls)}/{len(report.outcomes)} "
                f"ссылки={report.cited_share():.0%} auto={report.auto_share(high):.0%}",
                flush=True,
            )

    print_table(reports, high=high, repeats=args.repeats)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", default=list(DEFAULT_CANDIDATES))
    parser.add_argument("--phrases", type=int, default=len(_DEMO_PHRASES))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--pace", type=float, default=1.0, help="пауза между вызовами, с (лимиты провайдера)"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
