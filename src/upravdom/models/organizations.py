"""Организации и адресаты маршрутизации (architecture.md §5.1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from upravdom.models.base import Base, CreatedAtMixin, UUIDPKMixin
from upravdom.models.enums import ResourceType, pg_enum


class ManagementCompany(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "management_companies"

    name: Mapped[str] = mapped_column(String, nullable=False)
    region_code: Mapped[str] = mapped_column(String, nullable=False)
    ads_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    ads_hours: Mapped[str | None] = mapped_column(String, nullable=True)
    # Без значения по умолчанию: db-001.md — «организацию нельзя создать
    # без явного is_test_data» (техническая реализация требования кейса
    # явно обозначать смоделированные данные).
    is_test_data: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ResourceOrganization(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "resource_organizations"

    name: Mapped[str] = mapped_column(String, nullable=False)
    region_code: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[ResourceType] = mapped_column(
        pg_enum(ResourceType, name="ck_resource_organizations_resource_type"),
        nullable=False,
    )
    contact: Mapped[str | None] = mapped_column(String, nullable=True)
    is_test_data: Mapped[bool] = mapped_column(Boolean, nullable=False)


class HouseResourceOrg(UUIDPKMixin, Base):
    """Дом → ресурс → организация, версионируемо (architecture.md §5.1).

    Основная переменная часть при онбординге новой УК (idea_and_scope.md,
    чек-лист онбординга, шаг 2) — поэтому строка, а не одно поле на доме:
    контакт РСО меняется во времени, история должна сохраняться.
    """

    __tablename__ = "house_resource_orgs"

    house_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("houses.id"), nullable=False)
    resource_type: Mapped[ResourceType] = mapped_column(
        pg_enum(ResourceType, name="ck_house_resource_orgs_resource_type"),
        nullable=False,
    )
    resource_organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resource_organizations.id"), nullable=False
    )
    valid_from: Mapped[datetime] = mapped_column(nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(nullable=True)
