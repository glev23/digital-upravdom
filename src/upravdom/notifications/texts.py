"""Тексты уведомлений об отключениях (NOTIFY-001, architecture.md §5.3)."""

from __future__ import annotations

from datetime import datetime

from upravdom.models.enums import NotificationSource, NotificationType, ResourceType
from upravdom.tickets.due import local_short

RESOURCE_LABELS: dict[ResourceType, str] = {
    ResourceType.COLD_WATER: "холодное водоснабжение",
    ResourceType.HOT_WATER: "горячее водоснабжение",
    ResourceType.HEATING: "отопление",
    ResourceType.SEWAGE: "водоотведение",
    ResourceType.ELECTRICITY: "электроснабжение",
    ResourceType.GAS: "газоснабжение",
}

EMERGENCY = "Аварийное отключение: {resource}, ваш дом {address}. Начало — {starts}."
PLANNED = "Плановое отключение: {resource}, {address}, {starts} — {ends}."
PLANNED_OPEN_END = "Плановое отключение: {resource}, {address}, с {starts}, окончание уточняется."

# Ограничение №10 кейса: смоделированные данные обозначаются всегда — и в
# данных (`source`), и в том, что видит житель.
TEST_DATA_MARK = "(тестовые данные)"


def resource_label(resource: ResourceType) -> str:
    return RESOURCE_LABELS.get(resource, resource.value)


def notification_body(
    *,
    type_: NotificationType,
    resource: ResourceType,
    address: str,
    starts_at: datetime,
    ends_at: datetime | None,
    message: str,
    source: NotificationSource,
    tz_name: str,
) -> str:
    """Текст собирается из полей, а не берётся из свободного `message` целиком:
    ресурс, адрес и время должны быть одинаковыми у всех уведомлений, а
    `message` — только пояснение от УК/РСО."""

    resource_name = resource_label(resource)
    starts = local_short(starts_at, tz_name)
    if type_ is NotificationType.EMERGENCY_OUTAGE:
        head = EMERGENCY.format(resource=resource_name, address=address, starts=starts)
    elif ends_at is None:
        head = PLANNED_OPEN_END.format(resource=resource_name, address=address, starts=starts)
    else:
        head = PLANNED.format(
            resource=resource_name,
            address=address,
            starts=starts,
            ends=local_short(ends_at, tz_name),
        )

    parts = [head]
    if message.strip():
        parts.append(message.strip())
    if source is NotificationSource.TEST_DATA:
        parts.append(TEST_DATA_MARK)
    return " ".join(parts)
