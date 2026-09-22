"""Публичный API модуля заявок (TICKET-001, STATUS-001)."""

from __future__ import annotations

from upravdom.tickets.notify import notify_subscribers, subscriber_max_user_ids
from upravdom.tickets.routing import (
    PROBLEM_TO_RESOURCE,
    Addressee,
    org_display,
    resolve_addressee,
)
from upravdom.tickets.service import (
    InvalidStatusTransition,
    TicketClosed,
    TicketError,
    TicketNotFound,
    change_status,
    count_joined_subscribers,
    create_merged_ticket,
    create_ticket,
    get_by_number,
    get_by_source_event,
    get_for_user,
    list_for_user,
    list_history_events,
    reroute,
    split_merged,
)
from upravdom.tickets.texts import format_ticket_number, status_label

__all__ = [
    "PROBLEM_TO_RESOURCE",
    "Addressee",
    "InvalidStatusTransition",
    "TicketClosed",
    "TicketError",
    "TicketNotFound",
    "change_status",
    "count_joined_subscribers",
    "create_merged_ticket",
    "create_ticket",
    "format_ticket_number",
    "get_by_number",
    "get_by_source_event",
    "get_for_user",
    "list_for_user",
    "list_history_events",
    "notify_subscribers",
    "org_display",
    "reroute",
    "resolve_addressee",
    "split_merged",
    "status_label",
    "subscriber_max_user_ids",
]
