"""Идемпотентный сидер демо-состояния (DATA-001).

УК, РСО, дома, адресаты по ресурсам, deep-link токены, тестовые
уведомления — без этого ONBOARD-001 нечего привязывать (нет ни одного
дома), а TICKET-001 некуда маршрутизировать заявку.

Идемпотентность — тот же приём upsert'а, что и в `seed_problem_types.py`
(там — по натуральному ключу `code`), только здесь первичный ключ не
натуральный, поэтому id детерминирован через
`uuid5(SEED_NAMESPACE, "<бизнес-ключ>")` вместо случайного `uuid4`.
Все организации и дома — тестовые, `is_test_data = True` (кейс,
ограничение №10).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.config import get_settings
from upravdom.db import get_engine, get_session_factory
from upravdom.models import (
    House,
    HouseLink,
    HouseResourceOrg,
    ManagementCompany,
    Notification,
    ResourceOrganization,
)
from upravdom.models.enums import NotificationSource, NotificationType, ResourceType

# Фиксированное пространство имён — id стабилен между запусками и между
# разработчиками (не зависит от порядка вставки, только от строкового ключа).
SEED_NAMESPACE = uuid.UUID("a3f5c6d0-1234-4a11-9e00-000000000000")


def stable_id(key: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, key)


def deep_link_token(key: str) -> str:
    """Короткий ASCII-токен без спецсимволов — укладывается в лимит MAX-001
    (payload deep-link ≤128 символов, без кириллицы/URL-кодируемых символов)."""

    return stable_id(f"token:{key}").hex[:16]


MANAGEMENT_COMPANIES = [
    {
        "key": "uk-vahitovskaya",
        "name": "ООО «УК Вахитовская» (тест)",
        "region_code": "RU-TA",
        "ads_phone": "+7 (843) 000-00-01",
        "ads_hours": "круглосуточно",
    },
    {
        "key": "uk-privolzhskaya",
        "name": "ООО «УК Приволжская» (тест)",
        "region_code": "RU-TA",
        "ads_phone": "+7 (843) 000-00-02",
        "ads_hours": "пн-пт 8:00-20:00, авария — круглосуточно",
    },
]

RESOURCE_ORGANIZATIONS = [
    {
        "key": "vodokanal-cold",
        "name": "АО «Водоканал Казань» (тест)",
        "resource_type": ResourceType.COLD_WATER,
        "contact": "+7 (843) 000-00-11",
    },
    {
        "key": "vodokanal-sewage",
        "name": "АО «Водоканал Казань» (тест)",
        "resource_type": ResourceType.SEWAGE,
        "contact": "+7 (843) 000-00-11",
    },
    {
        "key": "tatenergo-hot",
        "name": "ПАО «Татэнерго» (тест)",
        "resource_type": ResourceType.HOT_WATER,
        "contact": "+7 (843) 000-00-12",
    },
    {
        "key": "tatenergo-heating",
        "name": "ПАО «Татэнерго» (тест)",
        "resource_type": ResourceType.HEATING,
        "contact": "+7 (843) 000-00-12",
    },
    {
        "key": "setevaya-electricity",
        "name": "АО «Сетевая компания» (тест)",
        "resource_type": ResourceType.ELECTRICITY,
        "contact": "+7 (843) 000-00-13",
    },
    {
        "key": "gazprom-gas",
        "name": "АО «Газпром газораспределение Казань» (тест)",
        "resource_type": ResourceType.GAS,
        "contact": "+7 (843) 000-00-14",
    },
]

HOUSES = [
    {
        "key": "house-dekabristov-10",
        "address_raw": "г. Казань, ул. Декабристов, д. 10",
        "region_code": "RU-TA",
        "management_company_key": "uk-vahitovskaya",
    },
    {
        "key": "house-lumumby-5",
        "address_raw": "г. Казань, ул. Патриса Лумумбы, д. 5",
        "region_code": "RU-TA",
        "management_company_key": "uk-privolzhskaya",
    },
    {
        "key": "house-gvardeyskaya-20",
        "address_raw": "г. Казань, ул. Гвардейская, д. 20",
        "region_code": "RU-TA",
        "management_company_key": "uk-vahitovskaya",
    },
]

# Каждый дом обслуживается всеми 6 РСО (один и тот же набор поставщиков
# ресурсов в пределах города — реалистично для демо в одном регионе).
HOUSE_RESOURCE_ORG_KEYS = [
    "vodokanal-cold",
    "vodokanal-sewage",
    "tatenergo-hot",
    "tatenergo-heating",
    "setevaya-electricity",
    "gazprom-gas",
]

# По две ссылки на дом — на стенд в подъезде и в квитанцию (idea_and_scope.md,
# сценарий пилота).
HOUSE_LINK_LABELS = ["подъезд", "квитанция"]


async def seed(session: AsyncSession) -> dict[str, list[str]]:
    """Возвращает собранные deep-link для лога/README, не только пишет в БД."""

    now = datetime.now(UTC)
    deep_links: dict[str, list[str]] = {}

    for mc in MANAGEMENT_COMPANIES:
        stmt = (
            pg_insert(ManagementCompany)
            .values(
                id=stable_id(f"mc:{mc['key']}"),
                name=mc["name"],
                region_code=mc["region_code"],
                ads_phone=mc["ads_phone"],
                ads_hours=mc["ads_hours"],
                is_test_data=True,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "name": mc["name"],
                    "region_code": mc["region_code"],
                    "ads_phone": mc["ads_phone"],
                    "ads_hours": mc["ads_hours"],
                    "is_test_data": True,
                },
            )
        )
        await session.execute(stmt)

    for ro in RESOURCE_ORGANIZATIONS:
        stmt = (
            pg_insert(ResourceOrganization)
            .values(
                id=stable_id(f"ro:{ro['key']}"),
                name=ro["name"],
                region_code="RU-TA",
                resource_type=ro["resource_type"],
                contact=ro["contact"],
                is_test_data=True,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "name": ro["name"],
                    "region_code": "RU-TA",
                    "resource_type": ro["resource_type"],
                    "contact": ro["contact"],
                    "is_test_data": True,
                },
            )
        )
        await session.execute(stmt)

    for house in HOUSES:
        house_id = stable_id(f"house:{house['key']}")
        mc_id = stable_id(f"mc:{house['management_company_key']}")
        stmt = (
            pg_insert(House)
            .values(
                id=house_id,
                address_raw=house["address_raw"],
                fias_id=None,
                region_code=house["region_code"],
                management_company_id=mc_id,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "address_raw": house["address_raw"],
                    "region_code": house["region_code"],
                    "management_company_id": mc_id,
                },
            )
        )
        await session.execute(stmt)

        for ro_key in HOUSE_RESOURCE_ORG_KEYS:
            ro = next(r for r in RESOURCE_ORGANIZATIONS if r["key"] == ro_key)
            hro_stmt = (
                pg_insert(HouseResourceOrg)
                .values(
                    id=stable_id(f"hro:{house['key']}:{ro_key}"),
                    house_id=house_id,
                    resource_type=ro["resource_type"],
                    resource_organization_id=stable_id(f"ro:{ro_key}"),
                    valid_from=now - timedelta(days=365),
                    valid_to=None,
                )
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "resource_organization_id": stable_id(f"ro:{ro_key}"),
                        "valid_from": now - timedelta(days=365),
                        "valid_to": None,
                    },
                )
            )
            await session.execute(hro_stmt)

        house_links: list[str] = []
        for label in HOUSE_LINK_LABELS:
            token = deep_link_token(f"{house['key']}:{label}")
            link_stmt = (
                pg_insert(HouseLink)
                .values(
                    id=stable_id(f"link:{house['key']}:{label}"),
                    house_id=house_id,
                    token=token,
                    label=label,
                    is_active=True,
                    revoked_at=None,
                )
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={"is_active": True, "revoked_at": None},
                )
            )
            await session.execute(link_stmt)
            house_links.append(token)
        deep_links[house["key"]] = house_links

    # Тестовые уведомления — плановое отключение на первом доме, аварийное
    # на втором (architecture.md §5.3, источник reference — "test_data").
    notifications = [
        {
            "key": "notif-planned-cold-water",
            "house_key": "house-dekabristov-10",
            "type": NotificationType.PLANNED_OUTAGE,
            "resource_type": ResourceType.COLD_WATER,
            "starts_at": now + timedelta(days=2),
            "ends_at": now + timedelta(days=2, hours=8),
            # Без конкретного времени в тексте: дату и время житель видит из
            # полей `starts_at`/`ends_at` (NOTIFY-001), а прошитая в строку
            # «2 дня с 09:00 до 17:00» с ними расходилась.
            "message": "Профилактические работы на сетях.",
        },
        {
            "key": "notif-emergency-electricity",
            "house_key": "house-lumumby-5",
            "type": NotificationType.EMERGENCY_OUTAGE,
            "resource_type": ResourceType.ELECTRICITY,
            "starts_at": now - timedelta(minutes=30),
            "ends_at": None,
            "message": "Ведутся аварийно-восстановительные работы.",
        },
    ]
    # `DO NOTHING`, а не `DO UPDATE` (NOTIFY-001): сид выполняется на каждом
    # старте контейнера, и перезапись `ends_at` «воскрешала» бы аварию,
    # завершённую через `scripts/notify.py close`, а сдвиг `starts_at`
    # заново рассылал бы плановое уведомление. Идемпотентность сохраняется:
    # повторный запуск по-прежнему не создаёт вторую строку.
    for n in notifications:
        stmt = (
            pg_insert(Notification)
            .values(
                id=stable_id(f"notif:{n['key']}"),
                house_id=stable_id(f"house:{n['house_key']}"),
                type=n["type"],
                resource_type=n["resource_type"],
                starts_at=n["starts_at"],
                ends_at=n["ends_at"],
                message=n["message"],
                source=NotificationSource.TEST_DATA,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.execute(stmt)

    return deep_links


async def main() -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        deep_links = await seed(session)
        await session.commit()

    print(f"seeded {len(MANAGEMENT_COMPANIES)} management companies")
    print(f"seeded {len(RESOURCE_ORGANIZATIONS)} resource organizations")
    print(f"seeded {len(HOUSES)} houses with resource links and 2 deep-links each")
    bot_username = get_settings().max_bot_username
    print("deep-links for onboarding testing (ONBOARD-001):")
    for house_key, tokens in deep_links.items():
        house = next(h for h in HOUSES if h["key"] == house_key)
        for label, token in zip(HOUSE_LINK_LABELS, tokens, strict=True):
            link = (
                f"https://max.ru/{bot_username}?start={token}" if bot_username else f"start={token}"
            )
            print(f"  {house['address_raw']} [{label}]: {link}")

    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
