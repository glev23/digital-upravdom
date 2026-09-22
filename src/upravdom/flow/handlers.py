"""Основной сценарий в MAX после онбординга (FLOW-001)."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import outbox
from upravdom.bot_gateway.inbox import ClaimedEvent, get_or_create_user
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.classifier import Branch, Clarification, ClassificationResult, classify
from upravdom.classifier.llm import LlmClient
from upravdom.config import Settings, get_settings
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
from upravdom.models.enums import ResponsibilityZone, TicketEventType
from upravdom.onboarding import service as onboarding_service
from upravdom.onboarding.callbacks import inline_keyboard
from upravdom.tickets import (
    count_joined_subscribers,
    create_ticket,
    format_ticket_number,
    get_for_user,
    list_for_user,
    list_history_events,
    org_display,
    resolve_addressee,
    status_label,
)
from upravdom.tickets import texts as ticket_texts

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


# Ссылка на акт внутри norm_reference: «ПП РФ №354, приложение 1, п.1»,
# «ПП РФ №491, п.2, подп. «е»». Всё остальное в справочнике — служебные пояснения
# («NULL», «см. db-001.md»), которые жителю показывать нельзя.
_NORM_CITE = re.compile(r"ПП РФ №\s?\d+(?:,\s*[^:;()]+?)?(?=[:;()]|\.\s+[А-ЯЁ]|\.$|$)")


def _norm_label(norm_reference: str) -> str:
    """Только сама норма; пустая строка, если ссылки на акт в справочнике нет."""

    match = _NORM_CITE.search(norm_reference)
    return match.group(0).strip() if match else ""


def _local_until(due_at: datetime, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Europe/Moscow")
    return due_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def _format_due(due_at: datetime | None, norm_reference: str, tz_name: str) -> str:
    norm = _norm_label(norm_reference)
    if due_at is None:
        return texts.NO_DUE_LINE.format(norm=norm) if norm else texts.NO_DUE_LINE_BARE
    return texts.DUE_LINE.format(until=_local_until(due_at, tz_name), norm=norm)


# Системы, для которых ПП №416 п. 13 задаёт сроки локализации/устранения аварии.
_ADS_DEADLINE_TYPES = frozenset({"cold_water", "hot_water", "sewage", "heating", "electricity"})


def _basis_line(result: ClassificationResult, norm_reference: str) -> str:
    if result.citations:
        cite = result.citations[0]
        snippet = " ".join(cite.body.split())[:120]
        return f"Основание: {cite.label}" + (f" — {snippet}…" if snippet else "")
    # Без подтверждённого чанка — только сама норма из справочника, не текст модели.
    short = _norm_label(norm_reference)
    return f"Основание: {short}" if short else "Основание: по справочнику типов проблем"


async def _org_display(
    session: AsyncSession, org_type: ResponsibilityZone | None, org_id: uuid.UUID | None
) -> tuple[str | None, str | None]:
    # Разбор полиморфного адресата переехал в tickets.routing (STATUS-001):
    # его же использует лента истории и уведомления подписчикам.
    return await org_display(session, org_type, org_id)


def _local_short(moment: datetime, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Europe/Moscow")
    return moment.astimezone(tz).strftime("%d.%m %H:%M")


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
        f"• {_local_short(event.created_at, tz_name)} — {await _history_event_text(session, event)}"
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

    ticket = await create_ticket(
        session,
        user_id=user_id,
        house_id=house_id,
        raw_text=raw_text,
        problem_type=result.problem_type if result.problem_type else "other",
        responsibility_zone=create_zone,
        confidence=float(result.confidence),
        source_event_id=source_event_id,
    )
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
    due = _format_due(ticket.due_at, norm_ref, settings.display_timezone)
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
        due = _format_due(ticket.due_at, norm, settings.display_timezone)
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


async def on_message(
    session: AsyncSession,
    event: ClaimedEvent,
    *,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
) -> None:
    """Содержательное сообщение: ack → статус-команда или classify → ответ."""

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
