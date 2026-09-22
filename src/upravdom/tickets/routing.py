"""Определение адресата заявки (TICKET-001, architecture.md §5.1)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.models import House, HouseResourceOrg, ManagementCompany, ResourceOrganization
from upravdom.models.enums import ResourceType, ResponsibilityZone

# problem_type → resource_type для зоны rso (совпадают по коду).
PROBLEM_TO_RESOURCE: dict[str, ResourceType] = {
    "cold_water": ResourceType.COLD_WATER,
    "hot_water": ResourceType.HOT_WATER,
    "heating": ResourceType.HEATING,
    "sewage": ResourceType.SEWAGE,
    "electricity": ResourceType.ELECTRICITY,
    "gas": ResourceType.GAS,
}


@dataclass(slots=True, frozen=True)
class Addressee:
    """Куда маршрутизировать и что показать жителю."""

    org_type: ResponsibilityZone | None
    org_id: uuid.UUID | None
    name: str | None
    contact: str | None
    hours: str | None
    # Почему ушли на УК вместо РСО / почему адресата нет.
    fallback_reason: str | None = None


async def resolve_addressee(
    session: AsyncSession,
    house_id: uuid.UUID,
    zone: ResponsibilityZone,
    problem_type: str,
    *,
    on: datetime | None = None,
) -> Addressee:
    """Адресат по зоне. owner/municipality → пустой результат без исключения."""

    on = on or datetime.now(UTC)

    if zone in (ResponsibilityZone.OWNER, ResponsibilityZone.MUNICIPALITY):
        return Addressee(
            org_type=None,
            org_id=None,
            name=None,
            contact=None,
            hours=None,
            fallback_reason=None,
        )

    house = await session.get(House, house_id)
    mc_id = house.management_company_id if house else None

    if zone is ResponsibilityZone.RSO:
        resource = PROBLEM_TO_RESOURCE.get(problem_type)
        if resource is None:
            uk = await _uk_addressee(session, mc_id)
            return Addressee(
                org_type=uk.org_type,
                org_id=uk.org_id,
                name=uk.name,
                contact=uk.contact,
                hours=uk.hours,
                fallback_reason=f"rso_no_resource_for_problem_type:{problem_type}",
            )
        row = (
            await session.execute(
                select(ResourceOrganization)
                .join(
                    HouseResourceOrg,
                    HouseResourceOrg.resource_organization_id == ResourceOrganization.id,
                )
                .where(
                    HouseResourceOrg.house_id == house_id,
                    HouseResourceOrg.resource_type == resource,
                    HouseResourceOrg.valid_from <= on,
                    or_(
                        HouseResourceOrg.valid_to.is_(None),
                        HouseResourceOrg.valid_to > on,
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return Addressee(
                org_type=ResponsibilityZone.RSO,
                org_id=row.id,
                name=row.name,
                contact=row.contact,
                hours=None,
            )
        uk = await _uk_addressee(session, mc_id)
        return Addressee(
            org_type=uk.org_type,
            org_id=uk.org_id,
            name=uk.name,
            contact=uk.contact,
            hours=uk.hours,
            fallback_reason=f"rso_link_missing:{resource.value}",
        )

    # uk и unknown → УК дома (для unknown — разбор диспетчером).
    return await _uk_addressee(session, mc_id)


async def org_display(
    session: AsyncSession, org_type: ResponsibilityZone | None, org_id: uuid.UUID | None
) -> tuple[str | None, str | None]:
    """Название и контакт адресата по полям заявки — `(name, contact)`.

    Адресат полиморфный (УК или РСО, без FK — см. `models/tickets.py`),
    поэтому разбор типа нужен и в ответе жителю (FLOW-001), и в истории с
    уведомлениями (STATUS-001) — одна функция на всех.
    """

    if org_id is None or org_type is None:
        return None, None
    if org_type is ResponsibilityZone.UK:
        mc = await session.get(ManagementCompany, org_id)
        return (mc.name, mc.ads_phone) if mc else (None, None)
    if org_type is ResponsibilityZone.RSO:
        ro = await session.get(ResourceOrganization, org_id)
        return (ro.name, ro.contact) if ro else (None, None)
    return None, None


async def _uk_addressee(session: AsyncSession, mc_id: uuid.UUID | None) -> Addressee:
    if mc_id is None:
        return Addressee(
            org_type=None,
            org_id=None,
            name=None,
            contact=None,
            hours=None,
            fallback_reason="house_without_management_company",
        )
    mc = await session.get(ManagementCompany, mc_id)
    if mc is None:
        return Addressee(
            org_type=None,
            org_id=None,
            name=None,
            contact=None,
            hours=None,
            fallback_reason="management_company_not_found",
        )
    return Addressee(
        org_type=ResponsibilityZone.UK,
        org_id=mc.id,
        name=mc.name,
        contact=mc.ads_phone,
        hours=mc.ads_hours,
    )
