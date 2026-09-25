"""QA-001: сквозной сценарий на продакшн-маршрутизации, два прохода подряд.

Путь жителя идёт через `default_dispatcher` — ту же маршрутизацию, что в
рабочем воркере: онбординг по deep-link, кнопка согласия, шлюз, основной
сценарий, склейка и выход из неё, уведомление подписчику. Подменены только
внешние зависимости: LLM (FakeLlmClient), поиск по KB (фиксированный
фрагмент) и семантический кэш (отключён — иначе тест писал бы в рабочую
коллекцию Qdrant решения под именем боевой модели).

Второй проход на том же состоянии БД должен дать ту же последовательность
ответов — это пункт кейса «стабильно работает при повторном прохождении».
"""

from __future__ import annotations

import itertools
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from scripts.seed_demo import seed
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import inbox
from upravdom.bot_gateway.dispatcher import default_dispatcher
from upravdom.bot_gateway.inbox import ClaimedEvent
from upravdom.bot_gateway.schemas import parse_webhook_payload
from upravdom.classifier.llm.fake import FakeLlmClient
from upravdom.classifier.schema import LlmClassification
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.models import Ticket, User
from upravdom.models.enums import ResponsibilityZone, TicketStatus
from upravdom.tickets import change_status

pytestmark = pytest.mark.asyncio

_ts = itertools.count(1_758_900_000_000, 1000)
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
COMPLAINT = "второй день из крана на кухне не идёт холодная вода"


def _bot_started(user: str, token: str) -> dict[str, Any]:
    return {
        "update_type": "bot_started",
        "timestamp": next(_ts),
        "chat_id": user,
        "user": {"user_id": user},
        "payload": token,
    }


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


async def _deliver(session: AsyncSession, payload: dict[str, Any]) -> None:
    parsed = parse_webhook_payload(payload)
    await inbox.record_inbound_event(
        session, max_event_id=parsed.event_id, payload=payload, max_user_id=parsed.max_user_id
    )
    event_id = await session.scalar(
        text(
            "UPDATE inbound_events SET status = 'processing', attempts = 1 "
            "WHERE max_event_id = :m RETURNING id"
        ),
        {"m": parsed.event_id},
    )
    assert event_id is not None
    await default_dispatcher.dispatch(session, ClaimedEvent(event_id, parsed.event_id, payload, 1))
    await inbox.mark_done(session, event_id)


async def _replies(session: AsyncSession, user: str) -> list[dict[str, Any]]:
    """Сообщения жителю в порядке постановки, без ответов на нажатия кнопок."""

    rows = await session.execute(
        text(
            "SELECT payload FROM outbound_messages WHERE max_chat_id = :c "
            "AND NOT (payload::jsonb ? 'callback_id') ORDER BY created_at, id"
        ),
        {"c": user},
    )
    return [r.payload for r in rows]


def _buttons(message: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for att in message.get("attachments") or []:
        if att.get("type") != "inline_keyboard":
            continue
        for row in att["payload"]["buttons"]:
            for button in row:
                out[button["text"]] = button["payload"]
    return out


async def _press(session: AsyncSession, user: str, label: str) -> None:
    """Нажать кнопку с подписью, содержащей `label`, в последнем сообщении с ней."""

    for message in reversed(await _replies(session, user)):
        for text_, payload in _buttons(message).items():
            if label.lower() in text_.lower():
                await _deliver(session, _callback(user, payload))
                return
    raise AssertionError(f"кнопка «{label}» не найдена у {user}")


def _shape(texts: list[str]) -> list[str]:
    """Транскрипт без номеров, дат и времени — для сравнения двух проходов."""

    return [re.sub(r"\d+", "#", t) for t in texts]


@pytest.fixture
def external_stubs(monkeypatch: pytest.MonkeyPatch) -> FakeLlmClient:
    decision = LlmClassification(
        problem_type="cold_water",
        responsibility_zone=ResponsibilityZone.UK,
        confidence=0.95,
        cited_fragments=[1],
        reasoning="тест",
        clarifying_question=None,
        clarifying_options=[],
    )
    llm = FakeLlmClient(responses=[decision] * 20)

    async def fixed_search(*_a: object, **_k: object) -> list[RetrievedChunk]:
        return [
            RetrievedChunk(
                chunk_id=uuid.uuid4(),
                ref="п. 5 абз. 1",
                text="ПП РФ №491, п. 5 абз. 1\nсети до первого отключающего устройства.",
                score=0.9,
                source_key="pp491",
            )
        ]

    async def no_cache(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("upravdom.classifier.service.get_llm_client", lambda: llm)
    monkeypatch.setattr("upravdom.classifier.service.search", fixed_search)
    monkeypatch.setattr("upravdom.classifier.service.lookup_cache", no_cache)
    monkeypatch.setattr("upravdom.classifier.service.store_cache", no_cache)
    return llm


async def _journey(session: AsyncSession, token: str) -> tuple[list[str], list[str]]:
    """Один полный проход двух жителей одного дома; возвращает их транскрипты."""

    resident = f"e2e-a-{uuid.uuid4().hex[:8]}"
    neighbour = f"e2e-b-{uuid.uuid4().hex[:8]}"

    # Житель: QR → распознанный адрес → согласие → жалоба → статус.
    await _deliver(session, _bot_started(resident, token))
    await _press(session, resident, "Согласен")
    await _deliver(session, _message(resident, COMPLAINT))
    head = (
        await session.execute(
            select(Ticket).join(User, User.id == Ticket.user_id).where(User.max_user_id == resident)
        )
    ).scalar_one()
    await _deliver(session, _message(resident, f"статус {head.number}"))

    # Сосед с той же жалобой присоединяется, затем выходит из склейки кнопкой.
    await _deliver(session, _bot_started(neighbour, token))
    await _press(session, neighbour, "Согласен")
    await _deliver(session, _message(neighbour, COMPLAINT))
    await _press(session, neighbour, "другая проблема")

    # Диспетчер берёт заявку жителя в работу и закрывает её.
    await change_status(session, head, TicketStatus.IN_PROGRESS)
    await change_status(session, head, TicketStatus.COMPLETED)

    # Закрыть и отдельную заявку соседа: следующий проход начинается с дома
    # без открытых заявок, как после завершённого демо-сценария.
    own = (
        await session.execute(
            select(Ticket)
            .join(User, User.id == Ticket.user_id)
            .where(User.max_user_id == neighbour, Ticket.status == TicketStatus.ACCEPTED)
        )
    ).scalar_one()
    await change_status(session, own, TicketStatus.COMPLETED)

    resident_texts = [m["text"] for m in await _replies(session, resident)]
    neighbour_texts = [m["text"] for m in await _replies(session, neighbour)]
    return resident_texts, neighbour_texts


async def test_full_scenario_twice_on_same_state(
    session: AsyncSession, external_stubs: FakeLlmClient
) -> None:
    for row in json.loads(_PROBLEM_TYPES.read_text(encoding="utf-8")):
        await session.execute(_UPSERT_PT, row)
    deep_links = await seed(session)
    token = next(iter(deep_links.values()))[0]

    first_resident, first_neighbour = await _journey(session, token)
    second_resident, second_neighbour = await _journey(session, token)

    # Содержательная проверка первого прохода.
    joined = "\n".join(first_resident)
    assert "Принял, определяю, кто отвечает" in joined
    assert re.search(r"Заявка № \d+ принята", joined)
    assert "Основание: ПП РФ №491, п. 5 абз. 1" in joined
    assert any("в работе" in t.lower() for t in first_resident), "нет уведомления о смене статуса"

    neighbour_joined = "\n".join(first_neighbour)
    assert "в вашем доме уже сообщили" in neighbour_joined
    assert "принята отдельно" in neighbour_joined
    # После выхода из склейки сосед не получает уведомлений по чужой заявке.
    assert not any("в работе" in t.lower() for t in first_neighbour)

    # Повторяемость: второй проход — та же последовательность ответов.
    assert _shape(second_resident) == _shape(first_resident)
    assert _shape(second_neighbour) == _shape(first_neighbour)
