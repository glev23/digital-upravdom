"""Публичный API модуля уведомлений (NOTIFY-001)."""

from __future__ import annotations

from upravdom.notifications.service import Delivery, claim_deliveries, dispatch, list_active

__all__ = ["Delivery", "claim_deliveries", "dispatch", "list_active"]
