"""Сервис классификации: кэш → RAG+LLM → прототипы (CLASSIFY-001/002)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from functools import partial

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.classifier.cache import lookup_cache, store_cache
from upravdom.classifier.llm import LlmClient, LlmError, OpenRouterClient, get_llm_client
from upravdom.classifier.prompt import (
    PROMPT_VERSION,
    ProblemTypeInfo,
    build_messages,
)
from upravdom.classifier.prototypes import classify_by_prototypes
from upravdom.classifier.schema import (
    Branch,
    Clarification,
    ClassificationResult,
    LlmClassification,
)
from upravdom.classifier.texts import clarifying_between_types
from upravdom.config import Settings, get_settings
from upravdom.knowledge.artifacts import load_manifest
from upravdom.knowledge.retrieval import RetrievedChunk, search
from upravdom.masking import mask
from upravdom.models import ClassificationLog, House, KnowledgeChunk, ProblemType
from upravdom.models.enums import ResponsibilityZone

logger = logging.getLogger(__name__)

SearchFn = Callable[..., Awaitable[list[RetrievedChunk]]]

_CONFIDENCE_EPS = 0.001


def _has_meaningful_text(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def confirm_citations(
    cited_fragments: Sequence[int], chunks: Sequence[RetrievedChunk]
) -> list[RetrievedChunk]:
    """Номера фрагментов (1..N) → сами фрагменты; несуществующий номер отбрасывается."""

    confirmed: list[RetrievedChunk] = []
    seen: set[int] = set()
    for number in cited_fragments:
        if number in seen or not 1 <= number <= len(chunks):
            continue
        confirmed.append(chunks[number - 1])
        seen.add(number)
    return confirmed


def select_branch(
    confidence: float,
    *,
    high: float,
    low: float,
    clarifying_question: str | None,
    clarifying_options: list[str],
    after_clarification: bool,
) -> Branch:
    if confidence >= high:
        return Branch.AUTO
    if confidence < low:
        return Branch.UNKNOWN
    if after_clarification:
        return Branch.UNKNOWN
    options = [o.strip() for o in clarifying_options if o.strip()]
    if clarifying_question and clarifying_question.strip() and 2 <= len(options) <= 4:
        return Branch.CLARIFY
    return Branch.UNKNOWN


def _unknown_result(
    *,
    fallback_used: bool,
    log_id: uuid.UUID | None = None,
    model_name: str | None = None,
) -> ClassificationResult:
    return ClassificationResult(
        problem_type="other",
        responsibility_zone=ResponsibilityZone.UNKNOWN,
        confidence=0.0,
        branch=Branch.UNKNOWN,
        citations=[],
        clarifying_question=None,
        clarifying_options=[],
        fallback_used=fallback_used,
        log_id=log_id,
        model_name=model_name,
    )


async def _load_problem_types(session: AsyncSession) -> list[ProblemTypeInfo]:
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
            code=row.code,
            title=row.title,
            default_responsibility_zone=row.default_responsibility_zone,
            norm_reference=row.norm_reference,
        )
        for row in rows
    ]


async def _house_company_id(session: AsyncSession, house_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(select(House.management_company_id).where(House.id == house_id))


def _kb_version() -> str | None:
    try:
        return str(load_manifest()["kb_version"])
    except (OSError, KeyError, TypeError, ValueError):
        return None


async def _chunks_by_ids(
    session: AsyncSession, chunk_ids: Sequence[str]
) -> list[RetrievedChunk] | None:
    """None — не все id найдены в текущей БЗ (промах кэша)."""

    if not chunk_ids:
        return None
    uuids: list[uuid.UUID] = []
    for raw in chunk_ids:
        try:
            uuids.append(uuid.UUID(str(raw)))
        except ValueError:
            return None
    rows = (
        (await session.execute(select(KnowledgeChunk).where(KnowledgeChunk.id.in_(uuids))))
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(set(uuids)):
        return None
    out: list[RetrievedChunk] = []
    for cid in uuids:
        row = by_id[cid]
        meta = row.chunk_meta or {}
        out.append(
            RetrievedChunk(
                chunk_id=row.id,
                ref=str(meta.get("ref") or ""),
                text=row.chunk_text,
                score=1.0,
            )
        )
    return out


async def _write_log(
    session: AsyncSession,
    *,
    inbound_event_id: uuid.UUID,
    message_masked: str,
    problem_type: str,
    responsibility_zone: ResponsibilityZone,
    confidence: float,
    cache_hit: bool,
    fallback_used: bool,
    used_chunk_ids: list[str] | None,
    model_name: str | None,
    latency_ms: int,
    clarification: dict[str, object] | None = None,
) -> uuid.UUID:
    log_id = uuid.uuid4()
    session.add(
        ClassificationLog(
            id=log_id,
            inbound_event_id=inbound_event_id,
            ticket_id=None,
            message_masked=message_masked,
            problem_type=problem_type,
            responsibility_zone=responsibility_zone,
            confidence=confidence,
            cache_hit=cache_hit,
            fallback_used=fallback_used,
            used_chunk_ids=used_chunk_ids,
            model_name=model_name,
            prompt_version=PROMPT_VERSION,
            kb_version=_kb_version(),
            latency_ms=latency_ms,
            clarification=clarification,
        )
    )
    await session.flush()
    return log_id


async def _from_prototypes(
    message_masked: str,
    *,
    chunks: list[RetrievedChunk],
    high: float,
    low: float,
    after_clarification: bool,
) -> ClassificationResult:
    # embed() синхронный и считается на CPU — не в event loop (как поиск KB).
    decision = await asyncio.to_thread(partial(classify_by_prototypes, message_masked, high=high))
    question: str | None = None
    options: list[str] = []
    if not after_clarification and decision.runner_up_type and low <= decision.confidence < high:
        question, options = clarifying_between_types(decision.problem_type, decision.runner_up_type)
    branch = select_branch(
        decision.confidence,
        high=high,
        low=low,
        clarifying_question=question,
        clarifying_options=options,
        after_clarification=after_clarification,
    )
    if branch != Branch.CLARIFY:
        question = None
        options = []
    # Чанки KB — справочные, не основание «по норме»; auto уже запрещён потолком.
    return ClassificationResult(
        problem_type=decision.problem_type,
        responsibility_zone=decision.responsibility_zone,
        confidence=decision.confidence,
        branch=branch,
        citations=list(chunks),
        clarifying_question=question,
        clarifying_options=options,
        fallback_used=True,
        model_name=None,
    )


async def classify(
    text: str,
    *,
    house_id: uuid.UUID,
    inbound_event_id: uuid.UUID,
    session: AsyncSession,
    clarification: Clarification | None = None,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
    search_fn: SearchFn | None = None,
    force_no_llm: bool = False,
) -> ClassificationResult:
    """Свободный текст → решение. Кэш → LLM → прототипы. Не бросает наружу."""

    # Рабочий путь (FLOW-001) передаёт тот же закэшированный объект настроек.
    explicit_settings = settings is not None and settings is not get_settings()
    settings = settings or get_settings()
    high = settings.classify_confidence_high
    low = settings.classify_confidence_low
    started = time.perf_counter()

    masked = mask(text).text
    message_masked = str(masked)

    async def _finish(
        result: ClassificationResult,
        *,
        model_name: str | None,
        used_chunk_ids: list[str] | None,
        fallback_used: bool,
        cache_hit: bool,
    ) -> ClassificationResult:
        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        clarification_payload: dict[str, object] | None = None
        if (
            result.branch is Branch.CLARIFY
            and result.clarifying_question
            and result.clarifying_options
        ):
            clarification_payload = {
                "question": result.clarifying_question,
                "options": list(result.clarifying_options),
            }
        log_id = await _write_log(
            session,
            inbound_event_id=inbound_event_id,
            message_masked=message_masked,
            problem_type=result.problem_type,
            responsibility_zone=result.responsibility_zone,
            confidence=result.confidence,
            cache_hit=cache_hit,
            fallback_used=fallback_used,
            used_chunk_ids=used_chunk_ids,
            model_name=model_name,
            latency_ms=latency_ms,
            clarification=clarification_payload,
        )
        return ClassificationResult(
            problem_type=result.problem_type,
            responsibility_zone=result.responsibility_zone,
            confidence=result.confidence,
            branch=result.branch,
            citations=list(result.citations),
            clarifying_question=result.clarifying_question,
            clarifying_options=list(result.clarifying_options),
            fallback_used=fallback_used,
            log_id=log_id,
            model_name=model_name,
        )

    if not _has_meaningful_text(text):
        return await _finish(
            _unknown_result(fallback_used=False),
            model_name=None,
            used_chunk_ids=None,
            fallback_used=False,
            cache_hit=False,
        )

    kb_ver = _kb_version() or ""
    primary_model = settings.openrouter_model or ""

    # --- семантический кэш (только первый проход) ---
    if clarification is None and primary_model and kb_ver:
        hit = await lookup_cache(
            message_masked,
            model_name=primary_model,
            prompt_version=PROMPT_VERSION,
            kb_version=kb_ver,
            threshold=settings.semantic_cache_threshold,
        )
        if hit is not None:
            citations = await _chunks_by_ids(session, hit.chunk_ids)
            if citations is not None:
                result = ClassificationResult(
                    problem_type=hit.problem_type,
                    responsibility_zone=hit.responsibility_zone,
                    confidence=hit.confidence,
                    branch=Branch.AUTO,
                    citations=citations,
                    fallback_used=False,
                    model_name=hit.model_name,
                )
                return await _finish(
                    result,
                    model_name=hit.model_name,
                    used_chunk_ids=[str(c.chunk_id) for c in citations],
                    fallback_used=False,
                    cache_hit=True,
                )

    search_impl = search_fn or search
    company_id = await _house_company_id(session, house_id)
    chunks: list[RetrievedChunk] = []
    try:
        chunks = await search_impl(
            message_masked,
            k=5,
            management_company_id=company_id,
        )
    except Exception:  # noqa: BLE001 — кэш/поиск пропускаем, прототипы остаются
        logger.warning("classify: поиск KB недоступен", exc_info=True)

    problem_types = await _load_problem_types(session)
    if not problem_types:
        logger.error("classify: справочник problem_types пуст")
        return await _finish(
            _unknown_result(fallback_used=False),
            model_name=None,
            used_chunk_ids=None,
            fallback_used=False,
            cache_hit=False,
        )
    known_codes = {pt.code for pt in problem_types}

    # --- принудительно / отказ LLM → прототипы ---
    if force_no_llm:
        result = await _from_prototypes(
            message_masked,
            chunks=chunks,
            high=high,
            low=low,
            after_clarification=clarification is not None,
        )
        return await _finish(
            result,
            model_name=None,
            used_chunk_ids=[str(c.chunk_id) for c in result.citations] or None,
            fallback_used=True,
            cache_hit=False,
        )

    # Общий клиент процесса; отдельный — только если вызывающий передал свои
    # настройки (скрипты, тесты), чтобы не смешивать их с рабочими.
    client: LlmClient = llm or (
        OpenRouterClient(settings=settings) if explicit_settings else get_llm_client()
    )
    messages = build_messages(
        masked,
        chunks=chunks,
        problem_types=problem_types,
        clarification=clarification,
    )
    try:
        llm_result = await client.complete_json(messages, schema=LlmClassification)
    except LlmError:
        logger.info("classify: отказ LLM → прототипы")
        result = await _from_prototypes(
            message_masked,
            chunks=chunks,
            high=high,
            low=low,
            after_clarification=clarification is not None,
        )
        return await _finish(
            result,
            model_name=None,
            used_chunk_ids=[str(c.chunk_id) for c in result.citations] or None,
            fallback_used=True,
            cache_hit=False,
        )

    data = llm_result.data
    assert isinstance(data, LlmClassification)

    problem_type = data.problem_type.strip()
    zone = data.responsibility_zone
    confidence = float(data.confidence)
    force_unknown_type = problem_type not in known_codes
    if force_unknown_type:
        problem_type = "other"
        zone = ResponsibilityZone.UNKNOWN

    citations = confirm_citations(data.cited_fragments, chunks)
    if not citations:
        confidence = min(confidence, high - _CONFIDENCE_EPS)

    options = [o.strip() for o in data.clarifying_options if o.strip()]
    question = data.clarifying_question.strip() if data.clarifying_question else None

    if force_unknown_type:
        branch = Branch.UNKNOWN
        question = None
        options = []
    else:
        branch = select_branch(
            confidence,
            high=high,
            low=low,
            clarifying_question=question,
            clarifying_options=options,
            after_clarification=clarification is not None,
        )
        if branch != Branch.CLARIFY:
            question = None
            options = []

    result = ClassificationResult(
        problem_type=problem_type,
        responsibility_zone=zone,
        confidence=confidence,
        branch=branch,
        citations=citations,
        clarifying_question=question,
        clarifying_options=options,
        # fallback_used — только «решение без LLM» (§6.2 п. 7). Ответ резервной
        # модели — это всё ещё LLM; какая модель ответила, видно по model_name.
        fallback_used=False,
        model_name=llm_result.model_name,
    )

    # Кэш: только auto первого прохода основной модели.
    if (
        branch is Branch.AUTO
        and clarification is None
        and not llm_result.fallback_model_used
        and primary_model
        and kb_ver
        and citations
    ):
        await store_cache(
            message_masked,
            problem_type=problem_type,
            responsibility_zone=zone,
            confidence=confidence,
            chunk_ids=[str(c.chunk_id) for c in citations],
            model_name=primary_model,
            prompt_version=PROMPT_VERSION,
            kb_version=kb_ver,
        )

    return await _finish(
        result,
        model_name=llm_result.model_name,
        used_chunk_ids=[str(c.chunk_id) for c in citations] or None,
        # fallback_used — только «решение без LLM» (§6.2 п. 7). Ответ резервной
        # модели — это всё ещё LLM; какая модель ответила, видно по model_name.
        fallback_used=False,
        cache_hit=False,
    )
