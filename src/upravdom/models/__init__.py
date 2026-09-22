"""19→20 таблиц из architecture.md §5.7. Импорт этого модуля регистрирует все
модели в `Base.metadata` — на него ссылается `db/migrations/env.py`."""

from __future__ import annotations

from upravdom.models.base import Base
from upravdom.models.knowledge import KnowledgeChunk, KnowledgeDocument
from upravdom.models.notifications import Notification, NotificationDelivery
from upravdom.models.observability import ClassificationLog, RightsLog
from upravdom.models.organizations import (
    HouseResourceOrg,
    ManagementCompany,
    ResourceOrganization,
)
from upravdom.models.queues import InboundEvent, OutboundMessage
from upravdom.models.tickets import ProblemType, Ticket, TicketEvent, TicketSubscriber
from upravdom.models.users import Consent, House, HouseLink, HouseRequest, User, UserHouse

__all__ = [
    "Base",
    "User",
    "House",
    "UserHouse",
    "HouseLink",
    "Consent",
    "HouseRequest",
    "ManagementCompany",
    "ResourceOrganization",
    "HouseResourceOrg",
    "ProblemType",
    "Ticket",
    "TicketEvent",
    "TicketSubscriber",
    "Notification",
    "NotificationDelivery",
    "InboundEvent",
    "OutboundMessage",
    "KnowledgeDocument",
    "KnowledgeChunk",
    "ClassificationLog",
    "RightsLog",
]
