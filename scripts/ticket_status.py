"""Служебная смена статуса заявки (TICKET-001).

Не HTTP — для демо через контейнер:

  docker compose exec app python scripts/ticket_status.py 1 in_progress
  docker compose exec app python scripts/ticket_status.py 1 completed --actor dispatcher
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from upravdom.db import session_scope
from upravdom.models.enums import TicketStatus
from upravdom.tickets.service import (
    InvalidStatusTransition,
    change_status,
    get_by_number,
)
from upravdom.tickets.texts import format_ticket_number, status_label


async def _run(number: int, status: str, actor: str) -> int:
    try:
        to_status = TicketStatus(status)
    except ValueError:
        print(f"неизвестный статус: {status}", file=sys.stderr)
        print("допустимо: " + ", ".join(s.value for s in TicketStatus), file=sys.stderr)
        return 2

    async with session_scope() as session:
        ticket = await get_by_number(session, number)
        if ticket is None:
            print(f"заявка {format_ticket_number(number)} не найдена", file=sys.stderr)
            return 1
        try:
            await change_status(session, ticket, to_status, actor=actor)
        except InvalidStatusTransition as exc:
            print(str(exc), file=sys.stderr)
            return 1
        await session.commit()
        print(
            f"{format_ticket_number(ticket.number)}: {status_label(ticket.status)}",
            flush=True,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Служебная смена статуса заявки")
    parser.add_argument("number", type=int, help="короткий номер заявки")
    parser.add_argument("status", type=str, help="целевой статус")
    parser.add_argument("--actor", default="dispatcher", help="кто меняет статус")
    args = parser.parse_args()
    return asyncio.run(_run(args.number, args.status, args.actor))


if __name__ == "__main__":
    sys.exit(main())
