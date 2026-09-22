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
