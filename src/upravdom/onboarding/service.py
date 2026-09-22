"""Привязка дома и согласие на ПДн (architecture.md §7.1, §11.1).

Функции не коммитят: всё, что обработчик сделал с событием (привязка,
согласие, освобождение удержанных сообщений, ответ жителю), фиксируется
одним commit вместе с отметкой события `done` — частично выполненный
онбординг (дом привязан, согласия нет) невозможен.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.models import Consent, House, HouseLink, HouseRequest, ManagementCompany, UserHouse
from upravdom.models.enums import HouseRequestStatus
from upravdom.onboarding.address import HouseMatch, match_houses


@dataclass(slots=True, frozen=True)
class HouseInfo:
    id: uuid.UUID
    address: str
    ads_phone: str | None


@dataclass(slots=True, frozen=True)
class ResolvedLink:
    house: HouseInfo
    label: str | None


@dataclass(slots=True, frozen=True)
class UserState:
    primary_house: HouseInfo | None
    house_ids: frozenset[uuid.UUID]
    has_valid_consent: bool


async def get_house(session: AsyncSession, house_id: uuid.UUID) -> HouseInfo | None:
    row = (
        await session.execute(
            select(House.id, House.address_raw, ManagementCompany.ads_phone)
            .outerjoin(ManagementCompany, ManagementCompany.id == House.management_company_id)
            .where(House.id == house_id)
        )
    ).one_or_none()
    return HouseInfo(row.id, row.address_raw, row.ads_phone) if row else None


async def list_houses(session: AsyncSession) -> list[tuple[uuid.UUID, str]]:
    rows = (await session.execute(select(House.id, House.address_raw))).all()
    return [(row.id, row.address_raw) for row in rows]


async def search_houses(session: AsyncSession, query: str) -> list[HouseMatch]:
    return match_houses(query, await list_houses(session))


async def create_house_request(
    session: AsyncSession, user_id: uuid.UUID, address_text: str
) -> uuid.UUID:
    request_id = uuid.uuid4()
    session.add(
        HouseRequest(
            id=request_id,
            user_id=user_id,
            address_text=address_text.strip(),
            status=HouseRequestStatus.NEW,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return request_id


async def resolve_token(session: AsyncSession, token: str) -> ResolvedLink | None:
    """Только действующая ссылка: отозванную (смена УК) молча не принимаем."""

    row = (
        await session.execute(
            select(House.id, House.address_raw, ManagementCompany.ads_phone, HouseLink.label)
            .join(HouseLink, HouseLink.house_id == House.id)
            .outerjoin(ManagementCompany, ManagementCompany.id == House.management_company_id)
            .where(
                HouseLink.token == token,
                HouseLink.is_active.is_(True),
                HouseLink.revoked_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return ResolvedLink(HouseInfo(row.id, row.address_raw, row.ads_phone), row.label)


async def has_valid_consent(session: AsyncSession, user_id: uuid.UUID, version: int) -> bool:
    found = await session.scalar(
        select(Consent.id).where(
            Consent.user_id == user_id,
            Consent.consent_version == version,
            Consent.revoked_at.is_(None),
        )
    )
    return found is not None


async def get_state(session: AsyncSession, user_id: uuid.UUID, version: int) -> UserState:
    rows = (
        await session.execute(
            select(UserHouse.house_id, UserHouse.is_primary).where(UserHouse.user_id == user_id)
        )
    ).all()
    primary_id = next((r.house_id for r in rows if r.is_primary), None)
    primary = await get_house(session, primary_id) if primary_id else None
    return UserState(
        primary_house=primary,
        house_ids=frozenset(r.house_id for r in rows),
        has_valid_consent=await has_valid_consent(session, user_id, version),
    )


async def bind_house(session: AsyncSession, user_id: uuid.UUID, house_id: uuid.UUID) -> None:
    """Первый дом — основной, следующие — дополнительные; повтор — не дубль."""

    has_primary = await session.scalar(
        select(UserHouse.house_id).where(UserHouse.user_id == user_id, UserHouse.is_primary)
    )
    await session.execute(
        pg_insert(UserHouse)
        .values(
            user_id=user_id,
            house_id=house_id,
            is_primary=has_primary is None,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["user_id", "house_id"])
    )


async def grant_consent(
    session: AsyncSession, user_id: uuid.UUID, *, version: int, source: str
) -> bool:
    """False — действующее согласие этой версии уже есть (повторное нажатие)."""

    if await has_valid_consent(session, user_id, version):
        return False
    session.add(
        Consent(
            id=uuid.uuid4(),
            user_id=user_id,
            consent_version=version,
            granted_at=datetime.now(UTC),
            source=source,
        )
    )
    await session.flush()
    return True


async def revoke_consent(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Отзыв — `revoked_at`, не удаление: доказательство согласия в прошлом (§11.1)."""

    result = await session.execute(
        update(Consent)
        .where(Consent.user_id == user_id, Consent.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    return bool(getattr(result, "rowcount", 0))
