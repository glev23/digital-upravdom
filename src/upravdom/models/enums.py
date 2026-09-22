"""Закрытые перечисления модели данных (DB-001).

Рендерятся как `VARCHAR + CHECK`, а не нативный Postgres `ENUM` — добавление
значения не требует `ALTER TYPE` и отдельной миграции блокировки таблицы.

`problem_type` в перечисления не входит: это открытый справочник
(`problem_types` таблица), а не закрытый enum — см. architecture.md §4.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


def pg_enum[E: StrEnum](enum_cls: type[E], *, name: str) -> SAEnum:
    """`VARCHAR + именованный CHECK` по значениям enum, не по именам членов.

    Без `values_callable` SQLAlchemy кладёт в CHECK и в БД `.name` членов
    (`'UK'`), а не `.value` (`'uk'`) — расхождение с документированными
    строчными кодами architecture.md §4 и с тем, что реально хранится/
    сравнивается в бизнес-логике. `name` обязателен и должен быть уникален
    в рамках таблицы: одинаковое имя на двух колонках одной таблицы —
    невалидный DDL (Postgres требует уникальности имени ограничения на
    таблицу), и Alembic autogenerate в этом случае тихо не создаёт CHECK
    вовсе, что на практике сложно диагностировать.
    """

    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        create_constraint=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class ResponsibilityZone(StrEnum):
    """Кто отвечает за проблему (architecture.md §4)."""

    UK = "uk"
    RSO = "rso"
    OWNER = "owner"
    MUNICIPALITY = "municipality"
    UNKNOWN = "unknown"


class ResourceType(StrEnum):
    """Коммунальный ресурс — для РСО, справочника адресатов и уведомлений."""

    COLD_WATER = "cold_water"
    HOT_WATER = "hot_water"
    HEATING = "heating"
    SEWAGE = "sewage"
    ELECTRICITY = "electricity"
    GAS = "gas"


class TicketStatus(StrEnum):
    """Статус заявки (architecture.md §5.2, §6.5)."""

    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    ROUTED_TO_CONTRACTOR = "routed_to_contractor"
    COMPLETED = "completed"
    MERGED = "merged"
    NEEDS_DISPATCHER = "needs_dispatcher"


class TicketEventType(StrEnum):
    """Закрытый перечень событий истории заявки (TICKET-001, DEDUP-001)."""

    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    ROUTED = "routed"
    SUBSCRIBER_JOINED = "subscriber_joined"
    # Выход жителя из склейки «Это другая проблема» (DEDUP-001, миграция 0007).
    SUBSCRIBER_LEFT = "subscriber_left"


class JoinReason(StrEnum):
    """Почему житель подписан на заявку (architecture.md §6.5)."""

    AUTHOR = "author"
    DEDUP_AUTO = "dedup_auto"
    MANUAL = "manual"


class InboundEventStatus(StrEnum):
    """architecture.md §3."""

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    # ONBOARD-001: сообщение пришло до согласия на ПДн — удержано, воркер его
    # не забирает; после согласия возвращается в `pending` (architecture.md §7.1).
    AWAITING_CONSENT = "awaiting_consent"


class OutboundMessageStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class NotificationType(StrEnum):
    PLANNED_OUTAGE = "planned_outage"
    EMERGENCY_OUTAGE = "emergency_outage"


class NotificationSource(StrEnum):
    TEST_DATA = "test_data"
    MANUAL = "manual"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class HouseRequestStatus(StrEnum):
    """Заявка жителя на подключение дома (ONBOARD-002)."""

    NEW = "new"
    CONNECTED = "connected"
    REJECTED = "rejected"


class IndexState(StrEnum):
    """architecture.md §9 — состояние индексации чанка в Qdrant."""

    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"
