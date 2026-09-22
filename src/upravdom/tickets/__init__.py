"""Публичный API модуля заявок (TICKET-001)."""

from __future__ import annotations

from upravdom.tickets.routing import PROBLEM_TO_RESOURCE, Addressee, resolve_addressee
from upravdom.tickets.service import (
    InvalidStatusTransition,
    TicketError,
    TicketNotFound,
    change_status,
    create_ticket,
    get_by_number,
    get_for_user,
    list_for_user,
)
from upravdom.tickets.texts import format_ticket_number, status_label

__all__ = [
    "PROBLEM_TO_RESOURCE",
    "Addressee",
    "InvalidStatusTransition",
    "TicketError",
    "TicketNotFound",
    "change_status",
    "create_ticket",
    "format_ticket_number",
    "get_by_number",
    "get_for_user",
    "list_for_user",
    "resolve_addressee",
    "status_label",
]
