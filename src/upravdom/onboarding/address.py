"""Нормализация и сопоставление адреса (ONBOARD-002).

Без ФИАС/DaData: номер дома — точное совпадение, улица — нечётко
(`difflib.SequenceMatcher` + частичное пересечение токенов).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher

STREET_SIMILARITY_THRESHOLD = 0.75
MAX_CANDIDATES = 5

_CITY_TOKENS = frozenset(
    {
        "г",
        "город",
        "республика",
        "респ",
        "область",
        "обл",
        "край",
        "район",
        "р-н",
        "казань",
        "татарстан",
        "рт",
    }
)
_STREET_TYPES = frozenset(
    {
        "ул",
        "улица",
        "пр",
        "пр-т",
        "проспект",
        "пер",
        "переулок",
        "б-р",
        "бульвар",
        "наб",
        "набережная",
        "пл",
        "площадь",
        "ш",
        "шоссе",
        "проезд",
        "тупик",
    }
)
_HOUSE_MARKERS = frozenset({"д", "дом", "вл", "владение"})
_CORPUS_MARKERS = frozenset({"к", "корп", "корпус", "стр", "строение"})

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_HOUSE_NUM_RE = re.compile(r"^(\d+[а-яa-z]?)(?:к|корп|стр)?(\d+[а-яa-z]?)?$", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class ParsedAddress:
    street: str
    house_number: str
    tokens: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class HouseMatch:
    house_id: uuid.UUID
    address_raw: str
    score: float


def _tokenize(raw: str) -> list[str]:
    text = raw.strip().lower().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return [t for t in text.split(" ") if t]


def _normalize_house_number(raw: str) -> str:
    text = raw.lower().replace("ё", "е").replace(" ", "")
    text = text.replace("корпус", "к").replace("корп", "к").replace("строение", "стр")
    m = _HOUSE_NUM_RE.match(text)
    if not m:
        return text
    base, corp = m.group(1), m.group(2)
    return f"{base}к{corp}" if corp else base


def _looks_like_house_number(tok: str) -> bool:
    return bool(_HOUSE_NUM_RE.match(tok.replace(" ", "")))


def normalize_address(raw: str) -> ParsedAddress | None:
    """Разбирает свободный ввод. None — нет номера дома или улицы (мусор)."""

    tokens = _tokenize(raw)
    if not tokens:
        return None

    house_number: str | None = None
    street_tokens: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _CITY_TOKENS or tok in _STREET_TYPES:
            i += 1
            continue
        if tok in _HOUSE_MARKERS:
            i += 1
            if i < len(tokens) and _looks_like_house_number(tokens[i]):
                house_number = _normalize_house_number(tokens[i])
                i += 1
            if i < len(tokens) and tokens[i] in _CORPUS_MARKERS and i + 1 < len(tokens):
                if house_number:
                    house_number = f"{house_number}к{tokens[i + 1]}"
                i += 2
            continue
        if tok in _CORPUS_MARKERS:
            i += 1
            continue
        street_tokens.append(tok)
        i += 1

    if house_number is None and street_tokens and _looks_like_house_number(street_tokens[-1]):
        house_number = _normalize_house_number(street_tokens.pop())

    if house_number is None or not street_tokens:
        return None

    return ParsedAddress(
        street=" ".join(street_tokens),
        house_number=house_number,
        tokens=tuple(street_tokens),
    )


# Название улицы почти всегда стоит прямо перед номером дома; всё, что левее
# (житель часто пишет адрес в конце описания проблемы), в адрес не входит.
_MAX_STREET_TOKENS = 4
_ADDRESS_MARKERS = frozenset({"адрес", "адресу", "живу", "проживаю"})
MAX_STORED_ADDRESS_CHARS = 60


def canonical_address(parsed: ParsedAddress) -> str:
    """Короткая форма «улица номер» для кнопок и `house_requests`.

    Не сырой ввод: он может быть длинным (payload кнопки ограничен) и содержать
    описание проблемы с ПДн, которые до согласия сохранять нельзя.
    """

    street = list(parsed.tokens)
    markers = [i for i, t in enumerate(street) if t in _ADDRESS_MARKERS]
    if markers and markers[-1] + 1 < len(street):
        street = street[markers[-1] + 1 :]
    tokens = [t.capitalize() for t in street[-_MAX_STREET_TOKENS:]]
    while tokens and len(" ".join([*tokens, parsed.house_number])) > MAX_STORED_ADDRESS_CHARS:
        tokens.pop(0)
    return " ".join([*tokens, parsed.house_number])[:MAX_STORED_ADDRESS_CHARS]


def parse_stored_address(address_raw: str) -> ParsedAddress | None:
    return normalize_address(address_raw)


def _street_score(query: ParsedAddress, candidate: ParsedAddress) -> float:
    ratio = SequenceMatcher(None, query.street, candidate.street).ratio()
    if not query.tokens or not candidate.tokens:
        return ratio
    hits = 0
    for qt in query.tokens:
        best = max(SequenceMatcher(None, qt, ct).ratio() for ct in candidate.tokens)
        substring = any(qt in ct or ct in qt for ct in candidate.tokens if len(qt) >= 4)
        if best >= 0.75 or substring:
            hits += 1
    return max(ratio, hits / len(query.tokens))


def match_houses(
    query_raw: str,
    houses: list[tuple[uuid.UUID, str]],
    *,
    threshold: float = STREET_SIMILARITY_THRESHOLD,
    limit: int = MAX_CANDIDATES,
) -> list[HouseMatch]:
    """Кандидаты: номер дома точно, улица ≥ threshold. Лучшие первыми."""

    query = normalize_address(query_raw)
    if query is None:
        return []

    matches: list[HouseMatch] = []
    for house_id, address_raw in houses:
        stored = parse_stored_address(address_raw)
        if stored is None or stored.house_number != query.house_number:
            continue
        score = _street_score(query, stored)
        if score >= threshold:
            matches.append(HouseMatch(house_id, address_raw, score))

    matches.sort(key=lambda m: (-m.score, m.address_raw))
    return matches[:limit]
