"""Сценарии FLOW-001 на Postgres + FakeLlmClient."""

from __future__ import annotations

import itertools
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from scripts.seed_demo import seed, stable_id
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import inbox
from upravdom.bot_gateway.inbox import ClaimedEvent
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.classifier.llm.fake import FakeLlmClient, timeout
from upravdom.classifier.schema import Intent, LlmClassification
from upravdom.config import get_settings
from upravdom.flow import texts
from upravdom.flow.callbacks import encode_answer, encode_none, encode_norm, encode_status
from upravdom.flow.handlers import on_callback, on_message
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.models import ClassificationLog, House, Ticket
from upravdom.models.enums import ResponsibilityZone, TicketStatus
from upravdom.onboarding import service as onboarding_service
from upravdom.onboarding.handlers import gated
from upravdom.tickets import format_ticket_number

pytestmark = pytest.mark.asyncio

_ts = itertools.count(1_758_560_000_000, 1000)
_PROBLEM_TYPES = Path(__file__).resolve().parents[2] / "data" / "seed" / "problem_types.json"
_UPSERT_PT = text(
    """
    INSERT INTO problem_types
        (code, title, default_responsibility_zone, resolution_hours, norm_reference)
    VALUES
        (:code, :title, :default_responsibility_zone, :resolution_hours, :norm_reference)
    ON CONFLICT (code) DO UPDATE SET
        title = EXCLUDED.title,
        default_responsibility_zone = EXCLUDED.default_responsibility_zone,
        resolution_hours = EXCLUDED.resolution_hours,
        norm_reference = EXCLUDED.norm_reference
    """
)


def _message(user: str, text_: str) -> dict[str, Any]:
    return {
        "update_type": "message_created",
        "timestamp": next(_ts),
        "message": {
            "sender": {"user_id": user},
            "recipient": {"chat_id": user},
            "body": {"mid": f"mid-{uuid.uuid4()}", "text": text_},
        },
    }


def _callback(user: str, payload: str) -> dict[str, Any]:
    ts = next(_ts)
    return {
        "update_type": "message_callback",
        "timestamp": ts,
        "callback": {
            "timestamp": ts,
            "callback_id": f"cb-{uuid.uuid4()}",
            "payload": payload,
            "user": {"user_id": user},
        },
        "message": {"recipient": {"chat_id": user}},
    }


async def _seed_pt(session: AsyncSession) -> None:
    for row in json.loads(_PROBLEM_TYPES.read_text(encoding="utf-8")):
        await session.execute(_UPSERT_PT, row)


async def _outbox_texts(session: AsyncSession, chat: str) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT payload FROM outbound_messages WHERE max_chat_id = :c "
            "AND NOT (payload::jsonb ? 'callback_id') ORDER BY created_at, id"
        ),
        {"c": chat},
    )
    return [r.payload["text"] for r in rows]


async def _deliver(
    session: AsyncSession,
    payload: dict[str, Any],
    handler: Any,
    *,
    attempts: int = 1,
) -> uuid.UUID:
    parsed = parse_webhook_payload(payload)
    await inbox.record_inbound_event(
        session, max_event_id=parsed.event_id, payload=payload, max_user_id=parsed.max_user_id
    )
    event_id = await session.scalar(
        text(
            "UPDATE inbound_events SET status = 'processing', attempts = :a "
            "WHERE max_event_id = :m RETURNING id"
        ),
        {"m": parsed.event_id, "a": attempts},
    )
    assert event_id is not None
    await handler(session, ClaimedEvent(event_id, parsed.event_id, payload, attempts))
    await inbox.mark_done(session, event_id)
    return uuid.UUID(str(event_id))


@pytest.fixture
async def onboarded(session: AsyncSession) -> tuple[str, uuid.UUID]:
    await _seed_pt(session)
    deep = await seed(session)
    token = next(iter(deep.values()))[0]
    link = await onboarding_service.resolve_token(session, token)
    assert link is not None
    user_key = f"flow-{uuid.uuid4().hex[:8]}"
    from upravdom.bot_gateway.inbox import get_or_create_user

    user = await get_or_create_user(session, user_key)
    await onboarding_service.bind_house(session, user.id, link.house.id)
    await onboarding_service.grant_consent(
        session, user.id, version=get_settings().consent_version, source="test"
    )
    await session.commit()
    return user_key, link.house.id


def _llm(
    *,
    problem_type: str = "cold_water",
    zone: ResponsibilityZone = ResponsibilityZone.UK,
    confidence: float = 0.95,
    cited_fragments: list[int] | None = None,
    clarifying_question: str | None = None,
    clarifying_options: list[str] | None = None,
    off_topic: bool = False,
    intent: Intent = Intent.COMPLAINT,
) -> LlmClassification:
    return LlmClassification(
        problem_type=problem_type,
        responsibility_zone=zone,
        confidence=confidence,
        cited_fragments=cited_fragments or [1],
        off_topic=off_topic,
        intent=intent,
        reasoning="тест",
        clarifying_question=clarifying_question,
        clarifying_options=clarifying_options or [],
    )


async def _fixed_search(*_a: object, **_k: object) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=uuid.uuid4(),
            ref="п. 5 абз. 1",
            # Как выдаёт чанкер KB: первая строка — полная ссылка с документом.
            text=(
                "ПП РФ №491, п. 5 абз. 1\n"
                "сети до первого отключающего устройства — общее имущество."
            ),
            score=0.9,
            source_key="pp491",
        )
    ]


def _handler(llm: FakeLlmClient) -> Any:
    async def handle(session: AsyncSession, event: ClaimedEvent) -> None:
        # Подмена search через monkeypatch в тесте; здесь только llm.
        await on_message(session, event, llm=llm)

    return gated(handle)


@pytest.fixture
def patch_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("upravdom.classifier.service.search", _fixed_search)


async def test_auto_uk_creates_ticket(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _house = onboarded
    llm = FakeLlmClient(responses=[_llm()])
    await _deliver(session, _message(user, "из стены за ванной сифонит"), _handler(llm))
    texts_out = await _outbox_texts(session, user)
    assert texts.ACK_RECEIVED in texts_out
    assert any("Заявка №" in t and "принята" in t for t in texts_out)
    assert any("Основание: ПП РФ №491, п. 5 абз. 1" in t for t in texts_out)
    ticket = (await session.execute(select(Ticket))).scalars().first()
    assert ticket is not None
    assert ticket.status is TicketStatus.ACCEPTED
    assert ticket.responsibility_zone is ResponsibilityZone.UK


async def test_auto_rso(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    llm = FakeLlmClient(responses=[_llm(zone=ResponsibilityZone.RSO, problem_type="cold_water")])
    await _deliver(session, _message(user, "во всём квартале нет холодной воды"), _handler(llm))
    ticket = (await session.execute(select(Ticket))).scalars().one()
    assert ticket.routed_to_org_type is ResponsibilityZone.RSO
    assert ticket.routed_to_org_id == stable_id("ro:vodokanal-cold")


async def test_owner_no_ticket(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    llm = FakeLlmClient(responses=[_llm(zone=ResponsibilityZone.OWNER, problem_type="cold_water")])
    await _deliver(session, _message(user, "течёт смеситель на кухне"), _handler(llm))
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 0
    out = await _outbox_texts(session, user)
    assert any("собственник" in t.lower() for t in out)


async def test_clarify_flow_with_llm(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    llm = FakeLlmClient(
        responses=[
            _llm(
                confidence=0.6,
                clarifying_question="Где течёт?",
                clarifying_options=["В квартире", "На стояке"],
            ),
            _llm(confidence=0.92, zone=ResponsibilityZone.UK),
        ]
    )

    async def cb_with_llm(session: AsyncSession, event: ClaimedEvent) -> None:
        await on_callback(session, event, llm=llm)

    await _deliver(session, _message(user, "капает непонятно"), _handler(llm))
    log = (await session.execute(select(ClassificationLog))).scalars().one()
    await _deliver(session, _callback(user, encode_answer(log.id, 1)), cb_with_llm)
    tickets = (await session.execute(select(Ticket))).scalars().all()
    assert len(tickets) == 1
    assert tickets[0].status is TicketStatus.ACCEPTED


async def test_dont_know_needs_dispatcher(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    llm = FakeLlmClient(
        responses=[
            _llm(
                confidence=0.55,
                clarifying_question="Что случилось?",
                clarifying_options=["Вода", "Отопление"],
            )
        ]
    )
    await _deliver(session, _message(user, "странный шум"), _handler(llm))
    log = (await session.execute(select(ClassificationLog))).scalars().one()
    await _deliver(session, _callback(user, encode_none(log.id)), on_callback)
    ticket = (await session.execute(select(Ticket))).scalars().one()
    assert ticket.status is TicketStatus.NEEDS_DISPATCHER
    out = await _outbox_texts(session, user)
    assert any("Не берусь" in t for t in out)


async def test_llm_error_dispatcher_ticket(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    # force_no_llm path via timeout → prototypes; ensure not auto.
    # Для гарантированного needs_dispatcher используем текст «непонятно» + timeout
    # и проверяем fallback_used в журнале.
    llm = FakeLlmClient(responses=[timeout()])
    await _deliver(session, _message(user, "абракадабра xyz"), _handler(llm))
    logs = (await session.execute(select(ClassificationLog))).scalars().all()
    assert logs
    assert any(log.fallback_used for log in logs)
    ticket = (await session.execute(select(Ticket))).scalars().one()
    assert ticket.status is TicketStatus.NEEDS_DISPATCHER


async def test_status_command_skips_llm(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    llm = FakeLlmClient(responses=[_llm()])
    await _deliver(session, _message(user, "нет холодной воды"), _handler(llm))
    calls_before = len(llm.calls)
    await _deliver(session, _message(user, "статус"), _handler(llm))
    assert len(llm.calls) == calls_before
    out = await _outbox_texts(session, user)
    assert any("Ваши заявки" in t or "Заявка №" in t for t in out)


async def test_status_foreign_hidden(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    llm = FakeLlmClient(responses=[_llm()])
    await _deliver(session, _message(user, "нет воды"), _handler(llm))
    ticket = (await session.execute(select(Ticket))).scalars().one()
    stranger = f"stranger-{uuid.uuid4().hex[:6]}"
    from upravdom.bot_gateway.inbox import get_or_create_user

    await get_or_create_user(session, stranger)
    # Привяжем stranger к тому же дому с согласием, но без подписки на заявку.
    house_id = stable_id("house:house-dekabristov-10")
    other = await get_or_create_user(session, stranger)
    await onboarding_service.bind_house(session, other.id, house_id)
    await onboarding_service.grant_consent(
        session, other.id, version=get_settings().consent_version, source="test"
    )
    await _deliver(
        session,
        _message(stranger, f"статус {ticket.number}"),
        _handler(FakeLlmClient(responses=[])),
    )
    out = await _outbox_texts(session, stranger)
    assert any(texts.STATUS_NOT_FOUND in t for t in out)


async def test_ack_only_on_first_attempt(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    llm = FakeLlmClient(responses=[_llm(), _llm()])
    await _deliver(session, _message(user, "нет воды попытка1"), _handler(llm), attempts=1)
    await _deliver(session, _message(user, "нет воды попытка2"), _handler(llm), attempts=2)
    outs = await _outbox_texts(session, user)
    assert outs.count(texts.ACK_RECEIVED) == 1


async def test_clarify_button_idempotent(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    llm = FakeLlmClient(
        responses=[
            _llm(
                confidence=0.6,
                clarifying_question="Где?",
                clarifying_options=["А", "Б"],
            ),
            _llm(confidence=0.95),
            _llm(confidence=0.95),
        ]
    )

    async def cb(session: AsyncSession, event: ClaimedEvent) -> None:
        await on_callback(session, event, llm=llm)

    await _deliver(session, _message(user, "капает"), _handler(llm))
    log = (await session.execute(select(ClassificationLog))).scalars().one()
    payload = encode_answer(log.id, 0)
    await _deliver(session, _callback(user, payload), cb)
    await _deliver(session, _callback(user, payload), cb)
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 1


async def test_status_button(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _h = onboarded
    llm = FakeLlmClient(responses=[_llm()])
    await _deliver(session, _message(user, "нет хвс"), _handler(llm))
    ticket = (await session.execute(select(Ticket))).scalars().one()
    await _deliver(session, _callback(user, encode_status(ticket.number)), on_callback)
    out = await _outbox_texts(session, user)
    assert any(format_ticket_number_in(t, ticket.number) for t in out)


def format_ticket_number_in(text: str, number: int) -> bool:
    return f"№ {number}" in text or str(number) in text


async def test_status_command_gets_no_ack(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID]
) -> None:
    """«статус» — не жалоба: «Принял, определяю, кто отвечает» на него неверно."""

    user, _house = onboarded
    llm = FakeLlmClient(responses=[])
    await _deliver(session, _message(user, "статус"), _handler(llm))
    texts_out = await _outbox_texts(session, user)
    assert texts.ACK_RECEIVED not in texts_out
    assert llm.calls == []


async def test_ticket_message_names_interruption_honestly(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Срок из справочника — допустимый перерыв (прил. 1 ПП №354), а не срок устранения;
    сроки устранения аварии — ПП №416 п. 13; служебные пояснения справочника жителю не видны."""

    user, _house = onboarded
    llm = FakeLlmClient(responses=[_llm()])
    await _deliver(session, _message(user, "из стены за ванной сифонит"), _handler(llm))
    reply = next(t for t in await _outbox_texts(session, user) if "принята" in t)
    assert "Допустимый перерыв по нормативу" in reply
    assert "Срок по нормативу" not in reply
    assert "ПП РФ №416, п. 13" in reply
    assert "Зона по умолчанию" not in reply
    assert "db-001" not in reply


# --- QA-001: ошибочные действия жителя не оставляют тупиков ------------------


async def _ticket_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(Ticket)) or 0)


@pytest.mark.parametrize(
    "variant", ["статус {n}", "Статус №{n}", "статус заявки {n}", "/status {n}"]
)
async def test_status_free_form_is_command(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None, variant: str
) -> None:
    user, _house = onboarded
    await _deliver(
        session, _message(user, "из стены сифонит"), _handler(FakeLlmClient(responses=[_llm()]))
    )
    ticket = (await session.execute(select(Ticket))).scalars().one()
    before = await _ticket_count(session)

    llm = FakeLlmClient(responses=[])
    await _deliver(session, _message(user, variant.format(n=ticket.number)), _handler(llm))

    assert llm.calls == []
    assert await _ticket_count(session) == before
    last = (await _outbox_texts(session, user))[-1]
    assert last.startswith(format_ticket_number(ticket.number))


async def test_status_without_number_lists_tickets(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None
) -> None:
    user, _house = onboarded
    llm = FakeLlmClient(responses=[])
    await _deliver(session, _message(user, "статус N"), _handler(llm))
    assert llm.calls == []
    assert await _ticket_count(session) == 0
    assert (await _outbox_texts(session, user))[-1] == texts.STATUS_EMPTY


async def test_status_huge_number_is_not_found(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None
) -> None:
    user, _house = onboarded
    await _deliver(session, _message(user, "статус 99999999999999"), _handler(FakeLlmClient()))
    assert (await _outbox_texts(session, user))[-1] == texts.STATUS_NOT_FOUND


@pytest.mark.parametrize("phrase", ["помощь", "/help", "Привет!", "что ты умеешь?", "меню"])
async def test_help_phrases_get_help_without_ticket(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None, phrase: str
) -> None:
    user, _house = onboarded
    llm = FakeLlmClient(responses=[])
    await _deliver(session, _message(user, phrase), _handler(llm))
    assert llm.calls == []
    assert await _ticket_count(session) == 0
    assert await _outbox_texts(session, user) == [texts.HELP]


async def test_greeting_with_complaint_is_classified(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None
) -> None:
    user, _house = onboarded
    llm = FakeLlmClient(responses=[_llm()])
    await _deliver(session, _message(user, "привет, течёт кран в подвале"), _handler(llm))
    assert len(llm.calls) == 1
    assert await _ticket_count(session) == 1


async def test_non_text_message_gets_hint(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None
) -> None:
    user, _house = onboarded
    payload = _message(user, "")
    del payload["message"]["body"]["text"]
    payload["message"]["body"]["attachments"] = [{"type": "image", "payload": {"url": "x"}}]
    llm = FakeLlmClient(responses=[])
    await _deliver(session, payload, _handler(llm))
    assert llm.calls == []
    assert await _outbox_texts(session, user) == [texts.NON_TEXT]


# --- FLOW-002: бытовой диалог не доходит до модели и не плодит заявки -------


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("спасибо", texts.SMALLTALK),
        ("ок", texts.SMALLTALK),
        ("как пользоваться", texts.HELP),
        (")))", texts.JUNK),
    ],
)
async def test_smalltalk_costs_no_llm_call_and_no_ticket(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
    phrase: str,
    expected: str,
) -> None:
    user, _house = onboarded
    llm = FakeLlmClient(responses=[])
    await _deliver(session, _message(user, phrase), _handler(llm))
    assert llm.calls == [], "бытовая реплика не должна стоить вызова модели"
    assert await _ticket_count(session) == 0
    assert await _outbox_texts(session, user) == [expected]


async def test_house_info_answers_with_bound_address(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None
) -> None:
    user, house_id = onboarded
    llm = FakeLlmClient(responses=[])
    await _deliver(session, _message(user, "какой у меня дом"), _handler(llm))
    assert llm.calls == []
    house = await session.get(House, house_id)
    assert house is not None
    reply = (await _outbox_texts(session, user))[-1]
    assert house.address_raw in reply
    assert await _ticket_count(session) == 0


async def test_complaint_with_greeting_still_creates_ticket(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None
) -> None:
    """Главная защита: жалоба, начатая вежливостью, не должна стать болтовнёй."""

    user, _house = onboarded
    llm = FakeLlmClient(responses=[_llm()])
    await _deliver(session, _message(user, "спасибо, но вода так и не идёт"), _handler(llm))
    assert len(llm.calls) == 1
    assert await _ticket_count(session) == 1


# --- FLOW-003: вопрос не про дом не превращается в заявку ------------------


async def test_off_topic_question_creates_no_ticket(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None
) -> None:
    user, _house = onboarded
    llm = FakeLlmClient(
        responses=[
            _llm(
                problem_type="other",
                zone=ResponsibilityZone.UNKNOWN,
                confidence=0.1,
                cited_fragments=[],
                off_topic=True,
            )
        ]
    )
    await _deliver(session, _message(user, "где купить танк"), _handler(llm))
    assert await _ticket_count(session) == 0
    assert (await _outbox_texts(session, user))[-1] == texts.OFF_TOPIC


async def test_vague_housing_complaint_still_creates_ticket(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None
) -> None:
    """Опасное направление: расплывчатая жалоба про дом не должна стать отказом."""

    user, _house = onboarded
    llm = FakeLlmClient(
        responses=[
            _llm(
                problem_type="other",
                zone=ResponsibilityZone.UNKNOWN,
                confidence=0.1,
                cited_fragments=[],
                off_topic=False,
            )
        ]
    )
    await _deliver(session, _message(user, "в доме что-то гудит по ночам"), _handler(llm))
    assert await _ticket_count(session) == 1


async def test_off_topic_flag_ignored_when_type_recognised(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID], patch_search: None
) -> None:
    """Если тип жилищный, ошибочному off_topic не верим — заявка создаётся."""

    user, _house = onboarded
    llm = FakeLlmClient(responses=[_llm(confidence=0.1, off_topic=True)])
    await _deliver(session, _message(user, "нет холодной воды"), _handler(llm))
    assert await _ticket_count(session) == 1


async def test_house_tickets_list_shows_neighbour_without_text(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """«Что в доме» — список по дому, а не по подписке (FLOW-004).

    Житель должен увидеть, что об аварии уже сообщили, но не текст соседа.
    """

    from upravdom.bot_gateway.inbox import get_or_create_user

    author, house_id = onboarded
    complaint = "в подвале хлещет вода из трубы"
    await _deliver(
        session, _message(author, complaint), _handler(FakeLlmClient(responses=[_llm()]))
    )
    ticket = (await session.execute(select(Ticket))).scalars().one()

    neighbour_key = f"neigh-{uuid.uuid4().hex[:6]}"
    neighbour = await get_or_create_user(session, neighbour_key)
    await onboarding_service.bind_house(session, neighbour.id, house_id)
    await onboarding_service.grant_consent(
        session, neighbour.id, version=get_settings().consent_version, source="test"
    )
    await _deliver(
        session, _message(neighbour_key, "что в доме"), _handler(FakeLlmClient(responses=[]))
    )

    out = await _outbox_texts(session, neighbour_key)
    listing = next(t for t in out if texts.HOUSE_TICKETS_PRIVACY in t)
    assert format_ticket_number(ticket.number) in listing
    assert "Отсутствие или перебои холодного водоснабжения" in listing
    assert "принята" in listing
    assert complaint not in listing, "текст обращения соседа показывать нельзя"
    # Ни подтверждения приёма, ни заявки: это вопрос, а не обращение.
    assert texts.ACK_RECEIVED not in out
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 1


async def test_house_tickets_list_empty_and_merged_hidden(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _house = onboarded
    await _deliver(session, _message(user, "что в доме"), _handler(FakeLlmClient(responses=[])))
    assert any(texts.HOUSE_TICKETS_EMPTY in t for t in await _outbox_texts(session, user))

    llm = FakeLlmClient(responses=[_llm(), _llm()])
    await _deliver(session, _message(user, "нет холодной воды в стояке"), _handler(llm))
    await _deliver(session, _message(user, "нет холодной воды в стояке"), _handler(llm))
    merged = (
        (await session.execute(select(Ticket).where(Ticket.status == TicketStatus.MERGED)))
        .scalars()
        .all()
    )

    await _deliver(session, _message(user, "что в доме"), _handler(FakeLlmClient(responses=[])))
    listing = [t for t in await _outbox_texts(session, user) if texts.HOUSE_TICKETS_PRIVACY in t][
        -1
    ]
    for dup in merged:
        assert format_ticket_number(dup.number) not in listing, "склейка — не отдельное обращение"


async def test_house_tickets_list_hides_internal_type_name(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Служебный `other` в списке — «Тип уточняется», а не название справочника."""

    user, _house = onboarded
    # Ветка «не уверен» сохраняет тип от модели: здесь она сама не определила его.
    llm = FakeLlmClient(responses=[_llm(confidence=0.2, problem_type="other")])
    await _deliver(session, _message(user, "что-то гудит по ночам непонятно где"), _handler(llm))
    await _deliver(session, _message(user, "что в доме"), _handler(FakeLlmClient(responses=[])))

    listing = [t for t in await _outbox_texts(session, user) if texts.HOUSE_TICKETS_PRIVACY in t][
        -1
    ]
    assert texts.HOUSE_TICKETS_OTHER in listing
    assert "не удалось классифицировать" not in listing


async def test_intent_routes_house_question_instead_of_ticket(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Формулировку, которую не поймал шаблон, разбирает модель (FLOW-005).

    Живой прогон 28.09.2026: «какие заявки ещё есть в доме» уходило в
    классификацию и создавало заявку диспетчеру.
    """

    user, _house = onboarded
    llm = FakeLlmClient(
        responses=[_llm(problem_type="other", confidence=0.2, intent=Intent.HOUSE_TICKETS)]
    )
    await _deliver(session, _message(user, "какие заявки ещё есть в доме"), _handler(llm))

    out = await _outbox_texts(session, user)
    assert any(texts.HOUSE_TICKETS_EMPTY in t or texts.HOUSE_TICKETS_PRIVACY in t for t in out)
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 0


async def test_intent_routes_my_tickets_question(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    user, _house = onboarded
    llm = FakeLlmClient(
        responses=[_llm(problem_type="other", confidence=0.2, intent=Intent.MY_TICKETS)]
    )
    await _deliver(session, _message(user, "а что там с моими обращениями"), _handler(llm))

    out = await _outbox_texts(session, user)
    assert any(texts.STATUS_EMPTY in t or texts.STATUS_LIST_HEADER in t for t in out)
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 0


async def test_intent_does_not_swallow_a_complaint(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Жалоба с вопросом внутри остаётся жалобой: тип определён — значит заявка."""

    user, _house = onboarded
    llm = FakeLlmClient(responses=[_llm(problem_type="cold_water", intent=Intent.HOUSE_TICKETS)])
    await _deliver(
        session, _message(user, "какие заявки в доме, у меня опять течёт"), _handler(llm)
    )
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 1


async def test_intent_rights_does_not_create_ticket(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Вопрос о правах без команды и без «?» уходит в справку, а не в заявку."""

    user, _house = onboarded
    # Второй ответ в очереди не задан: справка честно откажет, но заявки не будет.
    llm = FakeLlmClient(
        responses=[_llm(problem_type="other", confidence=0.2, intent=Intent.RIGHTS)]
    )
    await _deliver(
        session, _message(user, "а вообще за сколько чинят такое по нормативу"), _handler(llm)
    )
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 0


async def _buttons_of_last(session: AsyncSession, chat: str) -> list[str]:
    row = await session.execute(
        text(
            "SELECT payload FROM outbound_messages WHERE max_chat_id = :c "
            "AND NOT (payload::jsonb ? 'callback_id') ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        {"c": chat},
    )
    payload = row.scalar_one()
    out: list[str] = []
    for att in payload.get("attachments") or []:
        for btn_row in att["payload"]["buttons"]:
            out.extend(btn["text"] for btn in btn_row)
    return out


async def _kb_chunk(
    session: AsyncSession, chunk_text: str, *, company_id: uuid.UUID | None = None
) -> uuid.UUID:
    from datetime import date

    from upravdom.models import KnowledgeChunk, KnowledgeDocument

    doc = KnowledgeDocument(
        source_key=f"test-{uuid.uuid4().hex[:6]}",
        title="тест",
        version="1",
        effective_from=date(2020, 1, 1),
        management_company_id=company_id,
        checksum="x",
    )
    session.add(doc)
    await session.flush()
    chunk = KnowledgeChunk(document_id=doc.id, chunk_text=chunk_text)
    session.add(chunk)
    await session.flush()
    return chunk.id


async def test_ticket_reply_is_short_and_offers_full_norm(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """UX 29.09.2026: основание — короткая цитата, полный пункт по кнопке."""

    user, _house = onboarded
    llm = FakeLlmClient(responses=[_llm()])
    await _deliver(session, _message(user, "из стены за ванной сифонит"), _handler(llm))
    reply = next(t for t in await _outbox_texts(session, user) if "принята" in t)
    basis = next(line for line in reply.splitlines() if line.startswith("Основание:"))
    assert len(basis) < 260
    assert texts.BTN_NORM in await _buttons_of_last(session, user)
    assert texts.BTN_STATUS in await _buttons_of_last(session, user)


async def test_norm_button_sends_full_clause(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID]
) -> None:
    user, _house = onboarded
    tail = "окончание пункта, которое в сообщение о заявке не помещается"
    body = "В состав общего имущества включаются внутридомовые системы, " * 20 + tail
    chunk_id = await _kb_chunk(session, f"ПП РФ №491, п. 5 абз. 1\n{body}")
    await session.commit()

    await _deliver(session, _callback(user, encode_norm(chunk_id)), on_callback)
    out = await _outbox_texts(session, user)
    assert out[-1].startswith("ПП РФ №491, п. 5 абз. 1\n\n")
    assert out[-1].endswith(tail)


async def test_norm_button_hides_other_company_document(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID]
) -> None:
    """Документ УК виден только её жителям — и в поиске, и по подобранному id."""

    user, house_id = onboarded
    own = await session.scalar(select(House.management_company_id).where(House.id == house_id))
    other = stable_id("mc:uk-privolzhskaya")
    if other == own:
        other = stable_id("mc:uk-vahitovskaya")
    secret = "секретный пункт договора чужой УК"
    chunk_id = await _kb_chunk(session, f"Договор управления, п. 4.2\n{secret}", company_id=other)
    await session.commit()

    await _deliver(session, _callback(user, encode_norm(chunk_id)), on_callback)
    out = await _outbox_texts(session, user)
    assert out[-1] == texts.NORM_NOT_FOUND
    assert not any(secret in t for t in out)


async def test_norm_button_own_company_document_is_shown(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID]
) -> None:
    user, house_id = onboarded
    own = await session.scalar(select(House.management_company_id).where(House.id == house_id))
    assert own is not None
    chunk_id = await _kb_chunk(
        session, "Договор управления, п. 4.2\nмастер — два часа", company_id=own
    )
    await session.commit()

    await _deliver(session, _callback(user, encode_norm(chunk_id)), on_callback)
    assert (await _outbox_texts(session, user))[-1].endswith("мастер — два часа")


async def test_norm_button_unknown_chunk(
    session: AsyncSession, onboarded: tuple[str, uuid.UUID]
) -> None:
    user, _house = onboarded
    await _deliver(session, _callback(user, encode_norm(uuid.uuid4())), on_callback)
    assert (await _outbox_texts(session, user))[-1] == texts.NORM_NOT_FOUND


async def test_intent_consult_answers_without_ticket(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Шум в подъезде — совет, а не заявка УК (консультант, 29.09.2026)."""

    from upravdom.consult import texts as consult_texts
    from upravdom.consult.schema import LlmConsultAnswer

    user, _house = onboarded
    llm = FakeLlmClient(
        responses=[
            _llm(problem_type="other", zone=ResponsibilityZone.UNKNOWN, intent=Intent.CONSULT),
            LlmConsultAnswer(answer="Шумом по ночам занимается участковый."),
        ]
    )
    await _deliver(session, _message(user, "в подъезде орут по ночам"), _handler(llm))
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 0
    out = await _outbox_texts(session, user)
    assert "участковый" in out[-1]
    assert consult_texts.FOOTER in out[-1]


async def test_consult_needing_repair_becomes_ticket(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Консультант увидел работу для УК — заявка, а не совет «подайте сами»."""

    from upravdom.consult.schema import LlmConsultAnswer

    user, _house = onboarded
    llm = FakeLlmClient(
        responses=[
            _llm(problem_type="other", zone=ResponsibilityZone.UNKNOWN, intent=Intent.CONSULT),
            LlmConsultAnswer(needs_ticket=True),
        ]
    )
    await _deliver(session, _message(user, "кот нагадил в подъезде"), _handler(llm))
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 1


async def test_consult_unavailable_keeps_ticket(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Второй вызов модели упал — обращение не теряется: заявка, как раньше."""

    user, _house = onboarded
    llm = FakeLlmClient(
        responses=[
            _llm(problem_type="other", zone=ResponsibilityZone.UNKNOWN, intent=Intent.CONSULT),
            timeout(),
        ]
    )
    await _deliver(session, _message(user, "в подъезде орут по ночам"), _handler(llm))
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 1


async def test_consult_with_concrete_type_stays_complaint(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Несимметричное условие, как у FLOW-003/005: тип найден — значит заявка."""

    user, _house = onboarded
    llm = FakeLlmClient(responses=[_llm(problem_type="common_area", intent=Intent.CONSULT)])
    await _deliver(session, _message(user, "в подъезде темно и какие-то люди орут"), _handler(llm))
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 1


async def test_consult_skipped_when_window_is_spent(
    session: AsyncSession,
    onboarded: tuple[str, uuid.UUID],
    patch_search: None,
) -> None:
    """Классификация съела окно видимости — консультанта не зовём, сразу заявка.

    Прод, 29.09.2026: классификация на висящих резервах заняла 100 с из 120, и
    второй вызов с полным бюджетом пережил бы окно — сообщение обработалось бы
    повторно. Здесь окно ужато так, что времени на консультанта нет с начала.
    """

    from upravdom.consult.schema import LlmConsultAnswer

    user, _house = onboarded
    llm = FakeLlmClient(
        responses=[
            _llm(problem_type="other", zone=ResponsibilityZone.UNKNOWN, intent=Intent.CONSULT),
            LlmConsultAnswer(answer="не должно понадобиться"),
        ]
    )
    narrow = get_settings().model_copy(update={"inbound_visibility_timeout_seconds": 30})

    async def handle(session: AsyncSession, event: ClaimedEvent) -> None:
        await on_message(session, event, llm=llm, settings=narrow)

    await _deliver(session, _message(user, "в подъезде орут по ночам"), gated(handle))
    assert (await session.scalar(select(func.count()).select_from(Ticket))) == 1
    assert len(llm.calls) == 1  # только классификация
