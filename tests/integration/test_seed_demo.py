"""Интеграционные тесты DATA-001: идемпотентность, флаги, целостность."""

from __future__ import annotations

from scripts.seed_demo import (
    HOUSE_RESOURCE_ORG_KEYS,
    HOUSES,
    MANAGEMENT_COMPANIES,
    RESOURCE_ORGANIZATIONS,
    deep_link_token,
    seed,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.models import (
    House,
    HouseLink,
    HouseResourceOrg,
    ManagementCompany,
    Notification,
    ResourceOrganization,
)


async def _counts(session: AsyncSession) -> dict[str, int]:
    return {
        "management_companies": (
            await session.execute(select(func.count()).select_from(ManagementCompany))
        ).scalar_one(),
        "resource_organizations": (
            await session.execute(select(func.count()).select_from(ResourceOrganization))
        ).scalar_one(),
        "houses": (await session.execute(select(func.count()).select_from(House))).scalar_one(),
        "house_resource_orgs": (
            await session.execute(select(func.count()).select_from(HouseResourceOrg))
        ).scalar_one(),
        "house_links": (
            await session.execute(select(func.count()).select_from(HouseLink))
        ).scalar_one(),
        "notifications": (
            await session.execute(select(func.count()).select_from(Notification))
        ).scalar_one(),
    }


async def test_seed_is_idempotent(session: AsyncSession) -> None:
    await seed(session)
    await session.commit()
    first = await _counts(session)

    await seed(session)
    await session.commit()
    second = await _counts(session)

    assert first == second
    assert first["management_companies"] == len(MANAGEMENT_COMPANIES)
    assert first["resource_organizations"] == len(RESOURCE_ORGANIZATIONS)
    assert first["houses"] == len(HOUSES)
    assert first["house_resource_orgs"] == len(HOUSES) * len(HOUSE_RESOURCE_ORG_KEYS)
    assert first["house_links"] == len(HOUSES) * 2
    assert first["notifications"] == 2


async def test_organizations_marked_as_test_data(session: AsyncSession) -> None:
    await seed(session)
    await session.commit()

    mcs = (await session.execute(select(ManagementCompany))).scalars().all()
    ros = (await session.execute(select(ResourceOrganization))).scalars().all()

    assert mcs and all(mc.is_test_data for mc in mcs)
    assert ros and all(ro.is_test_data for ro in ros)


async def test_each_house_has_full_resource_coverage(session: AsyncSession) -> None:
    await seed(session)
    await session.commit()

    houses = (await session.execute(select(House))).scalars().all()
    for house in houses:
        links = (
            (
                await session.execute(
                    select(HouseResourceOrg).where(HouseResourceOrg.house_id == house.id)
                )
            )
            .scalars()
            .all()
        )
        resource_types = {link.resource_type for link in links}
        assert len(resource_types) == len(HOUSE_RESOURCE_ORG_KEYS)


async def test_each_house_has_active_deep_links(session: AsyncSession) -> None:
    await seed(session)
    await session.commit()

    houses = (await session.execute(select(House))).scalars().all()
    for house in houses:
        links = (
            (
                await session.execute(
                    select(HouseLink).where(
                        HouseLink.house_id == house.id, HouseLink.is_active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(links) >= 2
        for link in links:
            assert link.token.isascii()
            assert len(link.token) <= 128
            assert " " not in link.token


def test_deep_link_token_format() -> None:
    token = deep_link_token("house-dekabristov-10:подъезд")
    assert token.isascii()
    assert len(token) <= 128
    assert all(c.isalnum() for c in token)
