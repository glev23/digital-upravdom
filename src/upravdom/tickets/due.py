"""Формулировка нормативного срока для жителя (FLOW-001, переиспользует DEDUP-001).

Жила в `flow/handlers.py`; вынесена сюда, потому что сообщение о присоединении
к существующей заявке называет срок теми же словами, а копия разъехалась бы с
оригиналом при первой правке (DEDUP-001).
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from upravdom.tickets.texts import DUE_LINE, NO_DUE_LINE, NO_DUE_LINE_BARE

# Ссылка на акт внутри norm_reference: «ПП РФ №354, приложение 1, п.1»,
# «ПП РФ №491, п.2, подп. «е»». Всё остальное в справочнике — служебные пояснения
# («NULL», «см. db-001.md»), которые жителю показывать нельзя.
_NORM_CITE = re.compile(r"ПП РФ №\s?\d+(?:,\s*[^:;()]+?)?(?=[:;()]|\.\s+[А-ЯЁ]|\.$|$)")


def norm_label(norm_reference: str) -> str:
    """Только сама норма; пустая строка, если ссылки на акт в справочнике нет."""

    match = _NORM_CITE.search(norm_reference)
    return match.group(0).strip() if match else ""


def display_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Europe/Moscow")


def local_until(moment: datetime, tz_name: str) -> str:
    return moment.astimezone(display_zone(tz_name)).strftime("%d.%m.%Y %H:%M")


def local_short(moment: datetime, tz_name: str) -> str:
    """Короткая метка для ленты истории (STATUS-001)."""

    return moment.astimezone(display_zone(tz_name)).strftime("%d.%m %H:%M")


def format_due(due_at: datetime | None, norm_reference: str, tz_name: str) -> str:
    norm = norm_label(norm_reference)
    if due_at is None:
        return NO_DUE_LINE.format(norm=norm) if norm else NO_DUE_LINE_BARE
    return DUE_LINE.format(until=local_until(due_at, tz_name), norm=norm)
