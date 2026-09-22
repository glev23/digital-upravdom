"""Пользователи, дома, онбординг (architecture.md §5.1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from upravdom.models.base import Base, CreatedAtMixin, UUIDPKMixin
from upravdom.models.enums import HouseRequestStatus, pg_enum


class User(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "users"

    max_user_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class House(UUIDPKMixin, CreatedAtMixin, Base):
    __tablename__ = "houses"

    address_raw: Mapped[str] = mapped_column(String, nullable=False)
    fias_id: Mapped[str | None] = mapped_column(String, nullable=True)
    region_code: Mapped[str] = mapped_column(String, nullable=False)
    management_company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("management_companies.id"), nullable=True
    )


class UserHouse(Base):
    """Связь житель↔дом. Без id — составной ключ (architecture.md §5.1)."""

    __tablename__ = "user_houses"
    __table_args__ = (
        # Один основной дом на жителя (db-001.md, «Инварианты на уровне БД»).
        Index(
            "ix_user_houses_one_primary_per_user",
            "user_id",
            unique=True,
            postgresql_where="is_primary",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    house_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("houses.id"), primary_key=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class HouseLink(UUIDPKMixin, CreatedAtMixin, Base):
    """Deep-link токены привязки дома (architecture.md §7.1)."""

    __tablename__ = "house_links"

    house_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("houses.id"), nullable=False)
    token: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Consent(UUIDPKMixin, Base):
    """Согласие на обработку ПДн, версионируемое (architecture.md §11.1)."""

    __tablename__ = "consents"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    consent_version: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)


class HouseRequest(UUIDPKMixin, CreatedAtMixin, Base):
    """Заявка на подключение дома, которого ещё нет в боте (ONBOARD-002)."""

    __tablename__ = "house_requests"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    address_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[HouseRequestStatus] = mapped_column(
        pg_enum(HouseRequestStatus, name="ck_house_requests_status"),
        nullable=False,
        default=HouseRequestStatus.NEW,
    )
