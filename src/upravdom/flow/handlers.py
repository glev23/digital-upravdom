"""Основной сценарий в MAX после онбординга (FLOW-001)."""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import outbox
from upravdom.bot_gateway.inbox import ClaimedEvent, get_or_create_user
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.classifier import Branch, Clarification, ClassificationResult, classify
from upravdom.classifier.llm import LlmClient
from upravdom.config import Settings, get_settings
from upravdom.dedup import texts as dedup_texts
from upravdom.dedup.callbacks import encode_split
from upravdom.dedup.service import embed_message, find_candidate, subscribe_to_head
from upravdom.flow import texts
from upravdom.flow.callbacks import (
    FlowAction,
    decode,
    encode_answer,
    encode_none,
    encode_status,
)
from upravdom.models import (
    ClassificationLog,
    InboundEvent,
    ProblemType,
    Ticket,
    TicketEvent,
)
from upravdom.models.enums import (
    RefusalReason,
    ResponsibilityZone,
    TicketEventType,
    TicketStatus,
)
from upravdom.onboarding import service as onboarding_service
from upravdom.onboarding.callbacks import inline_keyboard
from upravdom.rights import texts as rights_texts
from upravdom.rights.detect import RightsQuestion
from upravdom.rights.detect import detect as detect_rights
from upravdom.rights.service import answer_question as answer_rights
from upravdom.tickets import (
    count_joined_subscribers,
    create_merged_ticket,
    create_ticket,
    format_ticket_number,
    get_by_source_event,
    get_for_user,
    list_for_user,
    list_history_events,
    org_display,
    resolve_addressee,
    status_label,
)
from upravdom.tickets import texts as ticket_texts
from upravdom.tickets.due import format_due, local_short, norm_label

logger = logging.getLogger(__name__)

_STATUS_CMD = re.compile(
    r"^(?:/status|статус|моя\s+заявка)(?:\s+(\d+))?$",
    re.IGNORECASE,
)


async def _send(
    session: AsyncSession,
    chat_id: str,
    text_: str,
    attachments: list[dict[str, object]] | None = None,
) -> None:
    await outbox.enqueue_message(session, chat_id=chat_id, text_=text_, attachments=attachments)


# Формулировка срока и разбор norm_reference — в `tickets/due.py`: те же слова
# использует сообщение о присоединении к существующей заявке (DEDUP-001).

# Системы, для которых ПП №416 п. 13 задаёт сроки локализации/устранения аварии.
_ADS_DEADLINE_TYPES = frozenset({"cold_water", "hot_water", "sewage", "heating", "electricity"})


def _basis_line(result: ClassificationResult, norm_reference: str) -> str:
    if result.citations:
        cite = result.citations[0]
        snippet = " ".join(cite.body.split())[:120]
        return f"Основание: {cite.label}" + (f" — {snippet}…" if snippet else "")
    # Без подтверждённого чанка — только сама норма из справочника, не текст модели.
    short = norm_label(norm_reference)
    return f"Основание: {short}" if short else "Основание: по справочнику типов проблем"


async def _org_display(
    session: AsyncSession, org_type: ResponsibilityZone | None, org_id: uuid.UUID | None
) -> tuple[str | None, str | None]:
    # Разбор полиморфного адресата переехал в tickets.routing (STATUS-001):
    # его же использует лента истории и уведомления подписчикам.
    return await org_display(session, org_type, org_id)


async def _history_event_text(session: AsyncSession, event: TicketEvent) -> str:
    """Одна строка ленты. Для `routed` — название адресата из payload события,
    а не текущее поле заявки: в истории должно остаться то, что было тогда."""

    if event.event_type != TicketEventType.ROUTED.value:
        return status_label(event.to_status)

    target = event.payload.get("to") if isinstance(event.payload, dict) else None
    if not isinstance(target, dict):
        return ticket_texts.HISTORY_ROUTED_BARE
    name = target.get("name")
    if not name:
        org_type = target.get("org_type")
        org_id = target.get("org_id")
        name, _contact = await org_display(
            session,
            ResponsibilityZone(org_type) if org_type else None,
            uuid.UUID(str(org_id)) if org_id else None,
        )
    if not name:
        return ticket_texts.HISTORY_ROUTED_BARE
    return ticket_texts.HISTORY_ROUTED.format(org=name)


async def _history_lines(session: AsyncSession, ticket: Ticket, tz_name: str) -> list[str]:
    events = await list_history_events(session, ticket.id)
    lines = [
        f"• {local_short(event.created_at, tz_name)} — {await _history_event_text(session, event)}"
        for event in events
    ]
    joined = await count_joined_subscribers(session, ticket.id)
    if joined:
        lines.append(f"• {ticket_texts.joined_line(joined)}")
    return [ticket_texts.HISTORY_HEADER, *lines] if lines else []


def _status_keyboard(ticket_number: int | None = None) -> list[dict[str, object]]:
    return inline_keyboard([[(texts.BTN_STATUS, encode_status(ticket_number))]])


def _clarify_keyboard(log_id: uuid.UUID, options: list[str]) -> list[dict[str, object]]:
    rows: list[list[tuple[str, str]]] = [
        [(opt[:64], encode_answer(log_id, i))] for i, opt in enumerate(options)
    ]
    rows.append([(texts.BTN_DONT_KNOW, encode_none(log_id))])
    return inline_keyboard(rows)


async def _link_log_ticket(
    session: AsyncSession, log_id: uuid.UUID | None, ticket_id: uuid.UUID
) -> None:
    if log_id is None:
        return
    await session.execute(
        update(ClassificationLog).where(ClassificationLog.id == log_id).values(ticket_id=ticket_id)
    )


def _merged_keyboard(merged_number: int, head_number: int) -> list[dict[str, object]]:
    """«Это другая проблема» обязательна рядом со статусом: ложная склейка
    дороже пропущенного дубля (architecture.md §6.5)."""

    return inline_keyboard(
        [
            [(dedup_texts.BTN_OTHER_PROBLEM, encode_split(merged_number))],
            [(texts.BTN_STATUS, encode_status(head_number))],
        ]
    )


async def _repeat_merged_reply(
    session: AsyncSession, *, chat_id: str, merged: Ticket, settings: Settings
) -> None:
    head = (
        await session.get(Ticket, merged.duplicate_of_ticket_id)
        if merged.duplicate_of_ticket_id
        else None
    )
    if head is None:
        return
    head_pt = await session.get(ProblemType, head.problem_type)
    due = format_due(
        head.due_at, head_pt.norm_reference if head_pt else "", settings.display_timezone
    )
    body = dedup_texts.JOINED.format(
        number=format_ticket_number(head.number), status=status_label(head.status)
    )
    await _send(session, chat_id, f"{body}\n{due}", _merged_keyboard(merged.number, head.number))


async def _try_merge(
    session: AsyncSession,
    *,
    chat_id: str,
    user_id: uuid.UUID,
    house_id: uuid.UUID,
    raw_text: str,
    problem_type: str,
    responsibility_zone: ResponsibilityZone,
    source_event_id: uuid.UUID,
    result: ClassificationResult,
    settings: Settings,
) -> bool:
    """True — обращение склеено с открытой заявкой, отдельная заявка не нужна.

    Любой сбой внутри — False и обычное создание заявки: ни один отказ этого
    модуля не блокирует основной сценарий (architecture.md §6.5).
    """

    embedding = await embed_message(raw_text)
    if embedding is None:
        return False

    candidate = await find_candidate(
        session,
        house_id=house_id,
        problem_type=problem_type,
        author_id=user_id,
        embedding=embedding,
        settings=settings,
    )
    if candidate is None:
        return False

    head = candidate.head
    merged = await create_merged_ticket(
        session,
        head=head,
        user_id=user_id,
        house_id=house_id,
        raw_text=raw_text,
        problem_type=problem_type,
        responsibility_zone=responsibility_zone,
        confidence=float(result.confidence),
        text_embedding=embedding,
        source_event_id=source_event_id,
    )
    # Журнал классификации указывает на собственную (merged) строку.
    await _link_log_ticket(session, result.log_id, merged.id)

    head_number = format_ticket_number(head.number)
    head_pt = await session.get(ProblemType, head.problem_type)
    due = format_due(
        head.due_at, head_pt.norm_reference if head_pt else "", settings.display_timezone
    )

    if candidate.author_already_subscribed:
        # Повтор того же жителя: второй подписки и второго события нет.
        body = dedup_texts.ALREADY_YOURS.format(
            number=head_number, status=status_label(head.status)
        )
    else:
        await subscribe_to_head(session, head=head, user_id=user_id, merged_number=merged.number)
        body = dedup_texts.JOINED.format(number=head_number, status=status_label(head.status))

    await _send(
        session,
        chat_id,
        f"{body}\n{due}",
        _merged_keyboard(merged.number, head.number),
    )
    return True


async def _respond_result(
    session: AsyncSession,
    *,
    chat_id: str,
    user_id: uuid.UUID,
    house_id: uuid.UUID,
    raw_text: str,
    source_event_id: uuid.UUID,
    result: ClassificationResult,
    settings: Settings,
) -> None:
    pt = await session.get(ProblemType, result.problem_type)
    norm_ref = pt.norm_reference if pt else ""
    zone = result.responsibility_zone

    if result.branch is Branch.CLARIFY and result.log_id and result.clarifying_question:
        assert result.clarifying_options
        await _send(
            session,
            chat_id,
            result.clarifying_question,
            _clarify_keyboard(result.log_id, result.clarifying_options),
        )
        return

    if result.branch is Branch.AUTO and zone is ResponsibilityZone.OWNER:
        addressee = await resolve_addressee(session, house_id, ResponsibilityZone.UK, "other")
        contact = addressee.contact or texts.NO_UK_CONTACT
        body = (
            f"{texts.OWNER_INTRO}\n{_basis_line(result, norm_ref)}\n"
            f"Если нужна платная помощь или авария затрагивает соседей — АДС УК: {contact}."
        )
        await _send(session, chat_id, body)
        return

    if result.branch is Branch.AUTO and zone is ResponsibilityZone.MUNICIPALITY:
        body = (
            f"{texts.MUNICIPALITY_INTRO}\n{_basis_line(result, norm_ref)}\n"
            f"{settings.fallback_contact_text}"
        )
        await _send(session, chat_id, body)
        return

    # auto uk/rso, unknown, или clarify «не знаю» → заявка
    create_zone = zone
    if result.branch is Branch.UNKNOWN:
        create_zone = ResponsibilityZone.UNKNOWN

    if create_zone in (ResponsibilityZone.OWNER, ResponsibilityZone.MUNICIPALITY):
        # Защита: для этих зон заявку не создаём (уже обработано выше для auto).
        await _send(session, chat_id, settings.fallback_contact_text)
        return

    addressee = await resolve_addressee(session, house_id, create_zone, result.problem_type)
    if addressee.org_id is None and create_zone is not ResponsibilityZone.UNKNOWN:
        # Дом без УК — как unknown с общим контактом.
        create_zone = ResponsibilityZone.UNKNOWN

    problem_type = result.problem_type if result.problem_type else "other"

    existing = await get_by_source_event(session, source_event_id)
    if existing is not None and existing.status is TicketStatus.MERGED:
        # Ретрай воркера по уже склеенному обращению: второй склейки и второй
        # подписки быть не должно, ответ повторяем тот же.
        await _repeat_merged_reply(session, chat_id=chat_id, merged=existing, settings=settings)
        return

    # Дедупликация — после классификации и до создания заявки (§6.5).
    if existing is None:
        merged = await _try_merge(
            session,
            chat_id=chat_id,
            user_id=user_id,
            house_id=house_id,
            raw_text=raw_text,
            problem_type=problem_type,
            responsibility_zone=create_zone,
            source_event_id=source_event_id,
            result=result,
            settings=settings,
        )
        if merged:
            return

    ticket = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text=raw_text,
        problem_type=problem_type,
        responsibility_zone=create_zone,
        confidence=float(result.confidence),
        source_event_id=source_event_id,
    )
    # Вектор обращения нужен, чтобы к этой заявке могли присоединиться соседи.
    if ticket.text_embedding is None:
        ticket.text_embedding = await embed_message(raw_text)
    await _link_log_ticket(session, result.log_id, ticket.id)

    number = format_ticket_number(ticket.number)
    if create_zone is ResponsibilityZone.UNKNOWN or result.branch is Branch.UNKNOWN:
        contact = addressee.contact or settings.fallback_contact_text
        body = f"{texts.UNKNOWN_DISPATCHER.format(number=number)}\nАДС: {contact}."
        await _send(session, chat_id, body, _status_keyboard(ticket.number))
        return

    name, phone = await _org_display(session, ticket.routed_to_org_type, ticket.routed_to_org_id)
    if name is None:
        name = addressee.name or "организация"
        phone = addressee.contact
    due = format_due(ticket.due_at, norm_ref, settings.display_timezone)
    phone_line = phone or texts.NO_UK_CONTACT
    lines = [
        f"Заявка {number} принята. Отвечает: {name}. Аварийная служба: {phone_line}.",
        due,
    ]
    if create_zone is ResponsibilityZone.UK and result.problem_type in _ADS_DEADLINE_TYPES:
        lines.append(texts.ADS_DEADLINES)
    lines.append(_basis_line(result, norm_ref))
    body = "\n".join(lines)
    await _send(session, chat_id, body, _status_keyboard(ticket.number))


async def _handle_status(
    session: AsyncSession,
    *,
    chat_id: str,
    user_id: uuid.UUID,
    number: int | None,
    settings: Settings,
) -> None:
    if number is not None:
        ticket = await get_for_user(session, number, user_id)
        if ticket is None:
            await _send(session, chat_id, texts.STATUS_NOT_FOUND)
            return
        name, _phone = await _org_display(
            session, ticket.routed_to_org_type, ticket.routed_to_org_id
        )
        pt = await session.get(ProblemType, ticket.problem_type)
        norm = pt.norm_reference if pt else ""
        due = format_due(ticket.due_at, norm, settings.display_timezone)
        head = (
            f"{format_ticket_number(ticket.number)} — {status_label(ticket.status)}. "
            f"Адресат: {name or '—'}. {due}"
        )
        history = await _history_lines(session, ticket, settings.display_timezone)
        await _send(session, chat_id, "\n".join([head, *history]))
        return

    tickets = await list_for_user(session, user_id, limit=5)
    if not tickets:
        await _send(session, chat_id, texts.STATUS_EMPTY)
        return
    lines = [texts.STATUS_LIST_HEADER]
    for t in tickets:
        name, _ = await _org_display(session, t.routed_to_org_type, t.routed_to_org_id)
        lines.append(
            f"• {format_ticket_number(t.number)} — {status_label(t.status)}"
            + (f" ({name})" if name else "")
        )
    await _send(session, chat_id, "\n".join(lines))


async def _handle_rights(
    session: AsyncSession,
    event: ClaimedEvent,
    *,
    chat_id: str,
    house_id: uuid.UUID,
    question: RightsQuestion,
    llm: LlmClient | None,
    settings: Settings,
) -> None:
    """Справка по нормативам: подтверждение, ответ или честный отказ."""

    if not question.text:
        await _send(session, chat_id, rights_texts.HINT_NO_QUESTION)
        return

    # Своё подтверждение: «определяю, кто отвечает» на вопрос о правах неверно.
    if event.attempts == 1:
        await _send(session, chat_id, rights_texts.ACK_SEARCHING)
        await session.commit()

    addressee = await resolve_addressee(session, house_id, ResponsibilityZone.UK, "other")
    contact = addressee.contact or rights_texts.NO_CONTACT

    answer = await answer_rights(
        question.text,
        inbound_event_id=event.id,
        session=session,
        management_company_id=addressee.org_id,
        llm=llm,
        settings=settings,
    )
    if answer.refusal_reason in (RefusalReason.LLM_UNAVAILABLE, RefusalReason.KB_UNAVAILABLE):
        await _send(session, chat_id, rights_texts.LLM_UNAVAILABLE.format(contact=contact))
        return
    if answer.refused:
        await _send(session, chat_id, rights_texts.REFUSED.format(contact=contact))
        return
    await _send(session, chat_id, rights_texts.format_answer(answer.answer, answer.labels))


async def on_message(
    session: AsyncSession,
    event: ClaimedEvent,
    *,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
) -> None:
    """Содержательное сообщение: статус-команда → вопрос о правах → ack → classify."""

    settings = settings or get_settings()
    parsed = parse_webhook_payload(event.payload)
    if not parsed.chat_id or not parsed.max_user_id:
        return

    user = await get_or_create_user(session, parsed.max_user_id)
    state = await onboarding_service.get_state(session, user.id, settings.consent_version)
    if state.primary_house is None:
        return

    raw = (parsed.text or "").strip()
    if not raw:
        return

    # Команда статуса — до подтверждения: «Принял, определяю, кто отвечает» на
    # «статус» было бы неверно, это не жалоба.
    status_match = _STATUS_CMD.match(raw)
    if status_match:
        num = int(status_match.group(1)) if status_match.group(1) else None
        await _handle_status(
            session,
            chat_id=parsed.chat_id,
            user_id=user.id,
            number=num,
            settings=settings,
        )
        return

    # Вопрос о правах — до классификации (RIGHTS-001): это не жалоба, заявку
    # заводить не нужно, и подтверждение у него своё.
    question = detect_rights(raw)
    if question is not None:
        await _handle_rights(
            session,
            event,
            chat_id=parsed.chat_id,
            house_id=state.primary_house.id,
            question=question,
            llm=llm,
            settings=settings,
        )
        return

    # Мгновенное подтверждение — отдельный commit до классификации.
    if event.attempts == 1:
        await _send(session, parsed.chat_id, texts.ACK_RECEIVED)
        await session.commit()

    result = await classify(
        raw,
        house_id=state.primary_house.id,
        inbound_event_id=event.id,
        session=session,
        llm=llm,
        settings=settings,
    )
    await _respond_result(
        session,
        chat_id=parsed.chat_id,
        user_id=user.id,
        house_id=state.primary_house.id,
        raw_text=raw,
        source_event_id=event.id,
        result=result,
        settings=settings,
    )


async def _original_text(session: AsyncSession, inbound_event_id: uuid.UUID) -> str | None:
    row = await session.get(InboundEvent, inbound_event_id)
    if row is None:
        return None
    parsed = parse_webhook_payload(row.payload)
    return (parsed.text or "").strip() or None


async def on_callback(
    session: AsyncSession,
    event: ClaimedEvent,
    *,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
) -> None:
    """Кнопки clf:* — уточнение и статус."""

    settings = settings or get_settings()
    parsed = parse_webhook_payload(event.payload)
    if parsed.callback_id and parsed.max_user_id:
        await outbox.enqueue_callback_answer(
            session,
            chat_id=parsed.chat_id or parsed.max_user_id,
            callback_id=parsed.callback_id,
            notification=texts.CALLBACK_ACK,
        )

    cb = decode(parsed.text)
    if cb is None or not parsed.max_user_id or not parsed.chat_id:
        return

    user = await get_or_create_user(session, parsed.max_user_id)

    if cb.action is FlowAction.STATUS:
        await _handle_status(
            session,
            chat_id=parsed.chat_id,
            user_id=user.id,
            number=cb.ticket_number,
            settings=settings,
        )
        return

    if cb.log_id is None:
        return

    log = await session.get(ClassificationLog, cb.log_id)
    if log is None:
        return

    inbound = await session.get(InboundEvent, log.inbound_event_id)
    if inbound is None or inbound.max_user_id != parsed.max_user_id:
        logger.warning("clf callback log_id не принадлежит пользователю")
        return

    state = await onboarding_service.get_state(session, user.id, settings.consent_version)
    if state.primary_house is None:
        return

    raw = await _original_text(session, log.inbound_event_id)
    if not raw:
        return

    clar = log.clarification or {}
    question = str(clar.get("question") or "")
    raw_options = clar.get("options")
    options = [str(o) for o in raw_options] if isinstance(raw_options, list) else []

    if cb.action is FlowAction.NONE:
        # «Не знаю» → сразу unknown без второго LLM.
        fake = ClassificationResult(
            problem_type="other",
            responsibility_zone=ResponsibilityZone.UNKNOWN,
            confidence=0.0,
            branch=Branch.UNKNOWN,
            citations=[],
            fallback_used=False,
            log_id=log.id,
        )
        await _respond_result(
            session,
            chat_id=parsed.chat_id,
            user_id=user.id,
            house_id=state.primary_house.id,
            raw_text=raw,
            source_event_id=log.inbound_event_id,
            result=fake,
            settings=settings,
        )
        return

    if cb.action is FlowAction.ANSWER:
        if cb.option_index is None or not (0 <= cb.option_index < len(options)):
            return
        answer = str(options[cb.option_index])
        result = await classify(
            raw,
            house_id=state.primary_house.id,
            inbound_event_id=log.inbound_event_id,
            session=session,
            clarification=Clarification(question=question or "уточнение", answer=answer),
            llm=llm,
            settings=settings,
        )
        await _respond_result(
            session,
            chat_id=parsed.chat_id,
            user_id=user.id,
            house_id=state.primary_house.id,
            raw_text=raw,
            source_event_id=log.inbound_event_id,
            result=result,
            settings=settings,
        )
