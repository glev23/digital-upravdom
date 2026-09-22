"""Ответ на вопрос о правах со ссылкой на норму (RIGHTS-001, §6.4).

Путь независим от классификации: жалобы он не трогает, и ни один его отказ
не затрагивает основной сценарий. Ответ строится **только** на
подтверждённых фрагментах базы знаний — выдуманная норма хуже отказа.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.classifier.llm import LlmClient, LlmError, OpenRouterClient, get_llm_client
from upravdom.classifier.service import confirm_citations
from upravdom.config import Settings, get_settings
from upravdom.knowledge.artifacts import load_manifest
from upravdom.knowledge.retrieval import RetrievedChunk, search
from upravdom.masking import mask
from upravdom.models import RightsLog
from upravdom.models.enums import RefusalReason
from upravdom.rights.prompt import PROMPT_VERSION, build_messages
from upravdom.rights.schema import LlmRightsAnswer

logger = logging.getLogger(__name__)

TOP_K = 5

# Номер акта («№ 354», «№354») или пункта («п. 31», «п.5», «пункт 13») в тексте
# ответа. Просто число («не дольше 8 часов») не считается ссылкой на норму.
_NORM_IN_ANSWER = re.compile(r"№\s?(\d+)|п\.\s?(\d+)|пункт\w*\s+(\d+)", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")


@dataclass(slots=True, frozen=True)
class RightsAnswer:
    """Результат для FLOW-001: либо ответ с основаниями, либо отказ."""

    refused: bool
    answer: str = ""
    citations: list[RetrievedChunk] = field(default_factory=list)
    refusal_reason: RefusalReason | None = None
    log_id: uuid.UUID | None = None
    model_name: str | None = None

    @property
    def labels(self) -> list[str]:
        return [chunk.label for chunk in self.citations]


def _kb_version() -> str | None:
    try:
        return str(load_manifest()["kb_version"])
    except (OSError, KeyError, TypeError, ValueError):
        return None


def mentions_unknown_norm(answer: str, citations: list[RetrievedChunk]) -> bool:
    """Модель проговорилась о норме, которой ей не показывали.

    Сверяются только номера: ссылку модель всё равно пересказывает своими
    словами (урок CLASSIFY-001), а вот «ПП №290» среди показанных фрагментов
    про ПП №354 и №416 — это выдумка, и такой ответ жителю показывать нельзя.
    Ссылку на уже подтверждённый пункт отказом не считаем: промпт просит её
    не писать, но сама по себе она не опасна.
    """

    allowed = {int(number) for chunk in citations for number in _DIGITS.findall(chunk.label)}
    for match in _NORM_IN_ANSWER.finditer(answer):
        number = next(group for group in match.groups() if group is not None)
        if int(number) not in allowed:
            return True
    return False


async def _write_log(
    session: AsyncSession,
    *,
    inbound_event_id: uuid.UUID,
    question_masked: str,
    refused: bool,
    refusal_reason: RefusalReason | None,
    used_chunk_ids: list[str] | None,
    model_name: str | None,
    latency_ms: int,
) -> uuid.UUID:
    log_id = uuid.uuid4()
    session.add(
        RightsLog(
            id=log_id,
            inbound_event_id=inbound_event_id,
            question_masked=question_masked,
            refused=refused,
            refusal_reason=refusal_reason,
            used_chunk_ids=used_chunk_ids,
            model_name=model_name,
            prompt_version=PROMPT_VERSION,
            kb_version=_kb_version(),
            latency_ms=latency_ms,
        )
    )
    await session.flush()
    return log_id


async def answer_question(
    question: str,
    *,
    inbound_event_id: uuid.UUID,
    session: AsyncSession,
    management_company_id: uuid.UUID | None = None,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
) -> RightsAnswer:
    """Вопрос → ответ по нормативам или честный отказ. Наружу не бросает."""

    explicit_settings = settings is not None and settings is not get_settings()
    settings = settings or get_settings()
    started = time.perf_counter()

    masked = mask(question).text
    question_masked = str(masked)

    async def _finish(
        *,
        refused: bool,
        reason: RefusalReason | None,
        answer: str = "",
        citations: list[RetrievedChunk] | None = None,
        model_name: str | None = None,
    ) -> RightsAnswer:
        citations = citations or []
        log_id = await _write_log(
            session,
            inbound_event_id=inbound_event_id,
            question_masked=question_masked,
            refused=refused,
            refusal_reason=reason,
            used_chunk_ids=[str(c.chunk_id) for c in citations] or None,
            model_name=model_name,
            latency_ms=max(1, int((time.perf_counter() - started) * 1000)),
        )
        return RightsAnswer(
            refused=refused,
            answer=answer,
            citations=citations,
            refusal_reason=reason,
            log_id=log_id,
            model_name=model_name,
        )

    try:
        chunks = await search(question_masked, k=TOP_K, management_company_id=management_company_id)
    except Exception as exc:  # noqa: BLE001 — недоступность Qdrant не ломает сценарий
        logger.warning("rights: поиск KB недоступен (%s)", type(exc).__name__)
        return await _finish(refused=True, reason=RefusalReason.KB_UNAVAILABLE)

    if not chunks:
        return await _finish(refused=True, reason=RefusalReason.NO_CITATIONS)

    # Общий клиент процесса: лимит частоты и размыкатель живут в экземпляре
    # (урок ревью CLASSIFY). Свой — только если вызывающий передал свои
    # настройки (скрипты, тесты).
    client: LlmClient = llm or (
        OpenRouterClient(settings=settings) if explicit_settings else get_llm_client()
    )
    try:
        result = await client.complete_json(
            build_messages(masked, chunks=chunks), schema=LlmRightsAnswer
        )
    except LlmError as exc:
        logger.info("rights: отказ LLM (%s)", type(exc).__name__)
        return await _finish(refused=True, reason=RefusalReason.LLM_UNAVAILABLE)

    data = result.data
    assert isinstance(data, LlmRightsAnswer)

    if not data.sufficient:
        return await _finish(
            refused=True, reason=RefusalReason.INSUFFICIENT, model_name=result.model_name
        )

    citations = confirm_citations(data.cited_fragments, chunks)
    if not citations or not data.answer.strip():
        return await _finish(
            refused=True, reason=RefusalReason.NO_CITATIONS, model_name=result.model_name
        )

    if mentions_unknown_norm(data.answer, citations):
        logger.info("rights: в ответе норма вне показанных фрагментов — отказ")
        return await _finish(
            refused=True,
            reason=RefusalReason.UNKNOWN_NORM_IN_ANSWER,
            citations=citations,
            model_name=result.model_name,
        )

    return await _finish(
        refused=False,
        reason=None,
        answer=data.answer,
        citations=citations,
        model_name=result.model_name,
    )
