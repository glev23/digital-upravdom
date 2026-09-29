"""Консультант: совет по ситуации про дом, которая не решается заявкой.

Вызывается только из роутинга по `intent = consult` (flow/handlers.py), то
есть после классификации. Любой отказ консультанта возвращает `ok = False`, и
обращение идёт прежним путём — в заявку. Потерять обращение нельзя; в худшем
случае житель получает заявку, как было до консультанта.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from upravdom.classifier.llm import LlmClient, LlmError, OpenRouterClient, get_llm_client
from upravdom.classifier.service import confirm_citations
from upravdom.config import Settings, get_settings
from upravdom.consult.prompt import PROMPT_VERSION, build_messages
from upravdom.consult.schema import LlmConsultAnswer
from upravdom.knowledge.retrieval import RetrievedChunk, search
from upravdom.masking import mask
from upravdom.rights.service import mentions_unknown_norm

logger = logging.getLogger(__name__)

TOP_K = 3

# Телефон — от пяти цифр подряд с разделителями. 112 разрешён промптом явно;
# любой другой номер модель могла выдумать, а житель по нему позвонит.
_PHONE = re.compile(r"(?:\+?\d[\s\-()]*){5,}")
_ALLOWED_NUMBERS = {"112"}
_URL = re.compile(r"https?://|www\.|\.ru\b", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class ConsultAnswer:
    ok: bool
    answer: str = ""
    citations: list[RetrievedChunk] = field(default_factory=list)
    reason: str | None = None
    model_name: str | None = None


def _has_contact(answer: str) -> bool:
    """Телефон или сайт в тексте — их консультанту показывать не давали."""

    if _URL.search(answer):
        return True
    for match in _PHONE.finditer(answer):
        digits = re.sub(r"\D", "", match.group())
        if digits not in _ALLOWED_NUMBERS:
            return True
    return False


async def answer_consult(
    text: str,
    *,
    inbound_event_id: uuid.UUID,
    management_company_id: uuid.UUID | None = None,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
    budget_s: float | None = None,
) -> ConsultAnswer:
    """Сообщение → совет или отказ (`ok = False`). Наружу не бросает."""

    explicit_settings = settings is not None and settings is not get_settings()
    settings = settings or get_settings()
    started = time.perf_counter()
    masked = mask(text).text

    def _refuse(reason: str, model_name: str | None = None) -> ConsultAnswer:
        logger.info(
            "consult: отказ %s event=%s за %d мс (%s)",
            reason,
            inbound_event_id,
            int((time.perf_counter() - started) * 1000),
            PROMPT_VERSION,
        )
        return ConsultAnswer(ok=False, reason=reason, model_name=model_name)

    # База знаний — необязательный контекст: норм о шуме и соседях в ней нет,
    # но если фрагмент по делу найдётся, ссылка на него будет подтверждённой.
    try:
        chunks = await search(str(masked), k=TOP_K, management_company_id=management_company_id)
    except Exception as exc:  # noqa: BLE001 — без поиска консультант всё равно отвечает
        logger.warning("consult: поиск KB недоступен (%s)", type(exc).__name__)
        chunks = []

    client: LlmClient = llm or (
        OpenRouterClient(settings=settings) if explicit_settings else get_llm_client()
    )
    try:
        result = await client.complete_json(
            build_messages(masked, chunks=chunks), schema=LlmConsultAnswer, budget_s=budget_s
        )
    except LlmError as exc:
        return _refuse(f"llm_{type(exc).__name__}")

    data = result.data
    assert isinstance(data, LlmConsultAnswer)
    if data.needs_ticket:
        # Классификатор счёл это советом, консультант видит работу для УК
        # («кот нагадил в подъезде» — уборка). Заявка дешевле ошибки: бот
        # оформит её сам, а не отправит жителя подавать её где-то ещё.
        return _refuse("needs_ticket", result.model_name)
    if not data.answer:
        return _refuse("empty", result.model_name)

    citations = confirm_citations(data.cited_fragments, chunks)
    if mentions_unknown_norm(data.answer, citations):
        return _refuse("unknown_norm", result.model_name)
    if _has_contact(data.answer):
        return _refuse("contact_in_answer", result.model_name)

    logger.info(
        "consult: ответ event=%s model=%s за %d мс (%s)",
        inbound_event_id,
        result.model_name,
        int((time.perf_counter() - started) * 1000),
        PROMPT_VERSION,
    )
    return ConsultAnswer(
        ok=True, answer=data.answer, citations=citations, model_name=result.model_name
    )
