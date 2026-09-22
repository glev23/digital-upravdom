"""Тексты статусов заявки для жителя (TICKET-001)."""

from __future__ import annotations

from upravdom.models.enums import TicketStatus

STATUS_LABELS: dict[TicketStatus, str] = {
    TicketStatus.ACCEPTED: "принята",
    TicketStatus.NEEDS_DISPATCHER: "передана диспетчеру для уточнения ответственного",
    TicketStatus.IN_PROGRESS: "в работе",
    TicketStatus.ROUTED_TO_CONTRACTOR: "передана подрядчику",
    TicketStatus.COMPLETED: "выполнена",
    TicketStatus.MERGED: "объединена с другой заявкой",
}


def status_label(status: TicketStatus) -> str:
    return STATUS_LABELS.get(status, status.value)


def format_ticket_number(number: int) -> str:
    return f"№ {number}"


# --- Нормативный срок (FLOW-001; тот же текст у DEDUP-001) -------------------
# В problem_types.resolution_hours — допустимая продолжительность перерыва
# (прил. 1 ПП №354), а не срок устранения; называем честно.

DUE_LINE = "Допустимый перерыв по нормативу — до {until} ({norm})."
NO_DUE_LINE = "Нормативный срок для этого типа не установлен ({norm})."
NO_DUE_LINE_BARE = "Нормативный срок для этого типа не установлен."


# --- Уведомления подписчикам (STATUS-001) -----------------------------------

BTN_STATUS = "Статус заявки"

NOTIFY_STATUS_CHANGED = "Заявка {number}: статус изменён — {status}."
NOTIFY_COMPLETED = (
    "Заявка {number} выполнена. Если проблема не решена — напишите, создам новую заявку."
)
NOTIFY_REROUTED = "Заявка {number} передана: {org}, телефон {contact}."
NOTIFY_REROUTED_NO_CONTACT = "Заявка {number} передана: {org}."
NOTIFY_REROUTED_NO_ORG = "Заявка {number} передана — адресата уточнит диспетчер вашей УК."


def status_changed_notice(number: int, status: TicketStatus) -> str:
    """Отдельный текст для `completed`: жителю нужно знать, что делать, если
    «выполнено» на бумаге, а проблема осталась."""

    if status is TicketStatus.COMPLETED:
        return NOTIFY_COMPLETED.format(number=format_ticket_number(number))
    return NOTIFY_STATUS_CHANGED.format(
        number=format_ticket_number(number), status=status_label(status)
    )


def rerouted_notice(number: int, org: str | None, contact: str | None) -> str:
    formatted = format_ticket_number(number)
    if org is None:
        return NOTIFY_REROUTED_NO_ORG.format(number=formatted)
    if not contact:
        return NOTIFY_REROUTED_NO_CONTACT.format(number=formatted, org=org)
    return NOTIFY_REROUTED.format(number=formatted, org=org, contact=contact)


# --- История заявки по номеру (STATUS-001) ----------------------------------

HISTORY_HEADER = "История:"
HISTORY_ROUTED = "передана: {org}"
HISTORY_ROUTED_BARE = "передана другому адресату"


def joined_line(count: int) -> str:
    """Присоединившиеся показываются числом: имена и идентификаторы других
    жителей в чужой ленте показывать нельзя (architecture.md §11)."""

    tail = count % 100
    if 11 <= tail <= 14:
        noun = "жителей"
    elif count % 10 == 1:
        noun = "житель"
    elif count % 10 in (2, 3, 4):
        noun = "жителя"
    else:
        noun = "жителей"
    verb = "присоединился" if count % 10 == 1 and tail != 11 else "присоединились"
    return f"к заявке {verb} ещё {count} {noun}"
