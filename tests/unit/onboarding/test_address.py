"""Нормализация и сопоставление адреса (ONBOARD-002)."""

from __future__ import annotations

import uuid

import pytest

from upravdom.onboarding.address import match_houses, normalize_address

H1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
H2 = uuid.UUID("22222222-2222-2222-2222-222222222222")
H3 = uuid.UUID("33333333-3333-3333-3333-333333333333")

DEMO = [
    (H1, "г. Казань, ул. Декабристов, д. 10"),
    (H2, "г. Казань, ул. Патриса Лумумбы, д. 5"),
    (H3, "г. Казань, ул. Гвардейская, д. 20"),
]


@pytest.mark.parametrize(
    ("raw", "street_part", "house"),
    [
        ("Декабристов 10", "декабристов", "10"),
        ("г. Казань, ул. Декабристов, д. 10", "декабристов", "10"),
        ("декабрисов 10", "декабрисов", "10"),
        ("Лумумбы 5", "лумумбы", "5"),
    ],
)
def test_normalize_extracts_street_and_number(raw: str, street_part: str, house: str) -> None:
    parsed = normalize_address(raw)
    assert parsed is not None
    assert parsed.house_number == house
    assert street_part in parsed.street


@pytest.mark.parametrize("raw", ["привет", "", "🏠", "Декабристов", "только улица без номера"])
def test_normalize_rejects_garbage(raw: str) -> None:
    assert normalize_address(raw) is None


def test_exact_house_number_required_even_if_street_matches() -> None:
    """Декабристов 12 не должен давать дом №10 ни при каком сходстве улицы."""

    assert match_houses("Декабристов 12", DEMO) == []


def test_finds_decabristov_with_typo() -> None:
    matches = match_houses("декабрисов 10", DEMO)
    assert [m.house_id for m in matches] == [H1]


def test_finds_lumumba_partial_token() -> None:
    matches = match_houses("Лумумбы 5", DEMO)
    assert [m.house_id for m in matches] == [H2]


def test_full_formal_address() -> None:
    matches = match_houses("г. Казань, ул. Декабристов, д. 10", DEMO)
    assert [m.house_id for m in matches] == [H1]


def test_unknown_street_not_found() -> None:
    assert match_houses("Пушкина 1", DEMO) == []


def test_canonical_address_is_short_and_drops_problem_text() -> None:
    from upravdom.onboarding.address import MAX_STORED_ADDRESS_CHARS, canonical_address

    raw = (
        "у нас в подъезде третий день течёт труба с потолка, помогите, "
        "меня зовут Иванова, адрес Академика Королёва 18"
    )
    parsed = normalize_address(raw)
    assert parsed is not None

    short = canonical_address(parsed)

    assert short.endswith("Королева 18")
    assert "Иванова" not in short and "труба" not in short
    assert len(short) <= MAX_STORED_ADDRESS_CHARS


def test_leave_address_button_survives_long_input() -> None:
    """Длинный ввод раньше давал payload > лимита разбора — кнопка молча не работала."""

    from upravdom.onboarding.address import canonical_address
    from upravdom.onboarding.callbacks import (
        Action,
        OnboardingCallback,
        RefKind,
        decode,
        encode,
        encode_address,
    )

    parsed = normalize_address("очень длинное описание проблемы " * 10 + "Академика Арбузова 12")
    assert parsed is not None
    payload = encode(
        OnboardingCallback(Action.LEAVE, RefKind.ADDR, encode_address(canonical_address(parsed)))
    )

    assert decode(payload) is not None
