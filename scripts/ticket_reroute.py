"""Служебная переадресация заявки между организациями (STATUS-001).

Не HTTP — для демо через контейнер (architecture.md §8):

  docker compose exec app python scripts/ticket_reroute.py 1 --zone rso
  docker compose exec app python scripts/ticket_reroute.py 1 --zone uk --reason "не наша сеть"

Статус заявки не меняется: переадресация и статус — разные оси. Подписчики
получают уведомление в той же транзакции.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from upravdom.db import session_scope
from upravdom.models.enums import ResponsibilityZone
from upravdom.tickets.routing import org_display
from upravdom.tickets.service import TicketError, get_by_number, reroute
from upravdom.tickets.texts import format_ticket_number

_ZONES = (ResponsibilityZone.UK.value, ResponsibilityZone.RSO.value)


async def _run(number: int, zone: str, org_id: str | None, actor: str, reason: str | None) -> int:
    async with session_scope() as session:
        ticket = await get_by_number(session, number)
        if ticket is None:
            print(f"заявка {format_ticket_number(number)} не найдена", file=sys.stderr)
            return 1
        try:
            await reroute(
                session,
                ticket,
                zone=ResponsibilityZone(zone),
                org_id=uuid.UUID(org_id) if org_id else None,
                actor=actor,
                reason=reason,
            )
        except TicketError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        name, contact = await org_display(
            session, ticket.routed_to_org_type, ticket.routed_to_org_id
        )
        await session.commit()
        print(
            f"{format_ticket_number(ticket.number)}: передана {name or '—'}"
            + (f", {contact}" if contact else ""),
            flush=True,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Служебная переадресация заявки")
    parser.add_argument("number", type=int, help="короткий номер заявки")
    parser.add_argument("--zone", required=True, choices=_ZONES, help="кому передаём")
    parser.add_argument(
        "--org-id",
        default=None,
        help="конкретная организация; по умолчанию — адресат по дому и ресурсу",
    )
    parser.add_argument("--actor", default="dispatcher", help="кто переадресует")
    parser.add_argument("--reason", default=None, help="причина — попадёт в историю заявки")
    args = parser.parse_args()
    return asyncio.run(_run(args.number, args.zone, args.org_id, args.actor, args.reason))


if __name__ == "__main__":
    sys.exit(main())
