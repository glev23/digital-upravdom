"""Маскирование ПДн в тексте обращения перед внешней моделью (MASK-001).

Чистая функция без состояния и сети. Оригиналы значений не возвращаются.
`MaskedText` — обязательный тип входа промпта для CLASSIFY-001 / RIGHTS-001:
немаскированный `str` нельзя передать в промпт без ошибки mypy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import NewType

MaskedText = NewType("MaskedText", str)

PLACEHOLDER_PHONE = "[ТЕЛЕФОН]"
PLACEHOLDER_EMAIL = "[EMAIL]"
PLACEHOLDER_APARTMENT = "[КВАРТИРА]"
PLACEHOLDER_FIO = "[ФИО]"
PLACEHOLDER_ID = "[НОМЕР]"

_CATEGORY_PHONE = "phone"
_CATEGORY_EMAIL = "email"
_CATEGORY_APARTMENT = "apartment"
_CATEGORY_FIO = "fio"
_CATEGORY_ID = "id_number"

_NAMES_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "masking" / "first_names.txt"

# Предметная область и частые слова — не ФИО.
_STOP_WORDS = frozenset(
    {
        "водоканал",
        "татэнерго",
        "газпром",
        "россети",
        "управляющая",
        "компания",
        "аварийная",
        "диспетчерская",
        "казань",
        "москва",
        "декабристов",
        "лумумбы",
        "гвардейская",
        "пушкина",
        "батарея",
        "батареи",
        "вода",
        "горячая",
        "холодная",
        "отопление",
        "канализация",
        "лифт",
        "крыша",
        "подвал",
        "стояк",
        "сифон",
        "протечка",
        "засор",
        "здравствуйте",
        "добрый",
        "пожалуйста",
        "помогите",
        "срочно",
        "сегодня",
        "вчера",
        "завтра",
        "проблема",
        "обращение",
        "заявка",
        "градусов",
        "градуса",
        "часов",
        "часа",
        "минут",
        "минуты",
        "дней",
        "дня",
        "метров",
        "метра",
        "литров",
        "литра",
        "этажей",
        "подъездов",
        "домов",
        "квартир",
        "нормативов",
        "постановлению",
        "постановления",
    }
)

# Совпадают с обычными словами («с августа», «была дана», «вера в…») —
# маскируются только с заглавной буквы и не в начале предложения.
_AMBIGUOUS_NAMES = frozenset(
    {
        "август",
        "августа",
        "ада",
        "дана",
        "вера",
        "злата",
        "кира",
        "клим",
        "лада",
        "лев",
        "лилия",
        "лука",
        "любовь",
        "марк",
        "надежда",
        "роман",
    }
)

_WORD = re.compile(r"[А-ЯЁа-яё]+")

_PATRONYMIC = re.compile(r"(?:овна|евна|ична|инична|ович|евич|ьич|ич)$", re.IGNORECASE)

_PHONE = re.compile(
    r"""
    (?<!\d)
    (?:
        (?:\+7|8)\s*[\-()]?\s*\d{3}\s*[\-()]?\s*\d{3}\s*[\-]?\s*\d{2}\s*[\-]?\s*\d{2}
        |
        (?:\+7|8)\d{10}
        |
        \b\d{3}\s*[\-]\s*\d{2}\s*[\-]\s*\d{2}\b
    )
    (?!\d)
    """,
    re.VERBOSE,
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

_SNILS = re.compile(r"\b\d{3}[-\s]?\d{3}[-\s]?\d{3}\s?\d{2}\b")
_PASSPORT = re.compile(r"\b\d{2}\s?\d{2}\s?\d{6}\b")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{8,}(?!\d)")

_APARTMENT = re.compile(
    r"""
    (?:
        \bкв\.?\s*\d{1,4}\b
        |
        \bквартира\s+(?:номер\s+)?\d{1,4}\b
        |
        \bквартире\s+(?:номер\s+)?\d{1,4}\b
        |
        \bв\s+\d{1,4}(?:-?[йяеойая]{1,3})?\s+квартире\b
        |
        \bиз\s+\d{1,4}-?(?:й|ой|ей)\b
        |
        \bиз\s+\d{1,4}\s+(?:кв\b|квартир)
        |
        \b\d{1,4}(?:-?[яая])\s+квартира\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_INITIALS_SURNAME_FIRST = re.compile(
    r"\b([А-ЯЁ][а-яё]{2,})\s+([А-ЯЁ])\.\s*([А-ЯЁ])\.(?=\s|$|[.,;!?])"
)
_INITIALS_NAME_FIRST = re.compile(r"\b([А-ЯЁ])\.\s*([А-ЯЁ])\.\s*([А-ЯЁ][а-яё]{2,})(?=\s|$|[.,;!?])")

# Сильные триггеры — после них идёт имя в любом регистре («меня зовут сергей»).
_FIO_CONTEXT_STRONG = re.compile(
    r"""
    (?:
        меня\s+зовут
        | зовут\s+меня
        | я\s*[—\-–:]\s*
        | с\s+уважением\s*,?\s*
        | контактное\s+лицо\s*[—\-–:]?\s*
    )
    \s*([А-ЯЁа-яё]{2,})
    (?=[\s,.;!?]|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Слабые триггеры — имя только с заглавной: «хочу спросить почему…» и
# «никто не пишет ответ» не должны превращаться в ФИО.
_FIO_CONTEXT_WEAK = re.compile(
    r"""
    (?i:перезвоните\s+(?:мне\s+)?|спросить\s+|пишет\s+|обращается\s+)
    ([А-ЯЁ][а-яё]{1,}|[А-ЯЁ]{2,})
    (?=[\s,.;!?]|$)
    """,
    re.VERBOSE,
)

_SURNAME_SUFFIX = re.compile(
    r"""
    [А-ЯЁа-яё]{3,}
    (?:
        ов|ова|овой|ову|овым|ове
        |ев|ева|евой|еву|евым|еве
        |ёв|ёва|ёвой|ёву|ёвым|ёве
        |ин|ина|иной|ину|иным|ине
        |ын|ына|ыной|ыну|ыным|ыне
        |ский|ская|ского|ской|скому|скую
        |цкий|цкая|цкого|цкой|цкому|цкую
        |енко|ук|юк
    )$
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Перед словом КАПСОМ: в тексте целиком заглавными регистр ничего не говорит,
# поэтому фамилия узнаётся только в позиции «у/от/к/для ФАМИЛИИ».
_CAPS_NAME_PREPOSITIONS = frozenset({"у", "от", "к", "для"})

_ADJACENT_FIO = re.compile(r"\[ФИО\](?:\s+\[ФИО\])+")


@dataclass(slots=True, frozen=True)
class MaskResult:
    """Результат маскирования: текст + счётчики без исходных значений."""

    text: MaskedText
    counts: dict[str, int]


@lru_cache
def _first_names() -> frozenset[str]:
    if not _NAMES_PATH.is_file():
        return frozenset()
    return frozenset(
        line.strip().casefold()
        for line in _NAMES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


def _empty_counts() -> dict[str, int]:
    return {
        _CATEGORY_PHONE: 0,
        _CATEGORY_EMAIL: 0,
        _CATEGORY_APARTMENT: 0,
        _CATEGORY_FIO: 0,
        _CATEGORY_ID: 0,
    }


def _sub_count(pattern: re.Pattern[str], text: str, placeholder: str) -> tuple[str, int]:
    count = 0

    def _repl(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return placeholder

    return pattern.sub(_repl, text), count


def _is_sentence_start(text: str, start: int) -> bool:
    prefix = text[:start].rstrip()
    if not prefix:
        return True
    return prefix[-1] in ".!?…"


def _is_title(word: str) -> bool:
    return word[:1].isupper() and (len(word) == 1 or word[1:].islower())


def _looks_like_name_part(word: str) -> bool:
    """Фамилия/отчество по форме слова — без учёта регистра и позиции."""

    low = word.casefold()
    if low in _STOP_WORDS:
        return False
    return bool(_SURNAME_SUFFIX.search(word) or _PATRONYMIC.search(word))


def _is_first_name(word: str, *, sentence_start: bool) -> bool:
    low = word.casefold()
    if low not in _first_names() or low in _STOP_WORDS:
        return False
    if low in _AMBIGUOUS_NAMES:
        return _is_title(word) and not sentence_start
    return True


def _mask_name_words(text: str) -> tuple[str, int]:
    """Имена, фамилии и отчества по словам.

    Слово считается частью ФИО, если это имя из словаря; или фамилия/отчество
    по окончанию — но только с заглавной буквы не в начале предложения
    («у Кузнецова», а не «у магазина» / «выпал кирпич»); или рядом стоит
    имя («петрова ольга петровна»); или это слово КАПСОМ после «у/от/к/для».
    Соседние части одного ФИО схлопываются в один плейсхолдер.
    """

    words = list(_WORD.finditer(text))
    if not words:
        return text, 0

    starts = [_is_sentence_start(text, w.start()) for w in words]
    first = [
        _is_first_name(w.group(0), sentence_start=s) for w, s in zip(words, starts, strict=True)
    ]
    marked = list(first)

    for i, w in enumerate(words):
        if marked[i]:
            continue
        word = w.group(0)
        if not _looks_like_name_part(word):
            continue
        near_name = (i > 0 and first[i - 1]) or (i + 1 < len(words) and first[i + 1])
        titled = _is_title(word) and not starts[i]
        caps_after_prep = (
            word.isupper()
            and len(word) >= 4
            and i > 0
            and words[i - 1].group(0).casefold() in _CAPS_NAME_PREPOSITIONS
        )
        if near_name or titled or caps_after_prep:
            marked[i] = True

    # Фамилия перед «имя отчество»: «петрова ольга петровна».
    for i in range(len(words) - 1):
        if not marked[i] and marked[i + 1] and first[i + 1]:
            if _looks_like_name_part(words[i].group(0)):
                marked[i] = True

    out: list[str] = []
    count = 0
    pos = 0
    i = 0
    while i < len(words):
        if not marked[i]:
            i += 1
            continue
        j = i
        # Склеиваем подряд идущие части, если между ними только пробелы.
        while (
            j + 1 < len(words)
            and marked[j + 1]
            and not text[words[j].end() : words[j + 1].start()].strip()
        ):
            j += 1
        out.append(text[pos : words[i].start()])
        out.append(PLACEHOLDER_FIO)
        pos = words[j].end()
        count += 1
        i = j + 1
    out.append(text[pos:])
    return "".join(out), count


def _mask_fio(text: str) -> tuple[str, int]:
    total = 0

    text, n = _sub_count(_INITIALS_SURNAME_FIRST, text, PLACEHOLDER_FIO)
    total += n
    text, n = _sub_count(_INITIALS_NAME_FIRST, text, PLACEHOLDER_FIO)
    total += n

    def ctx_repl(m: re.Match[str]) -> str:
        nonlocal total
        name_start = m.start(1) - m.start(0)
        total += 1
        return m.group(0)[:name_start] + PLACEHOLDER_FIO

    text = _FIO_CONTEXT_STRONG.sub(ctx_repl, text)
    text = _FIO_CONTEXT_WEAK.sub(ctx_repl, text)

    text, n = _mask_name_words(text)
    total += n
    # «обращается Анна Сергеевна Кузнецова» → один плейсхолдер, а не два подряд.
    return _ADJACENT_FIO.sub(PLACEHOLDER_FIO, text), total


def mask(text: str) -> MaskResult:
    """Маскирует ПДн; смысл для классификации сохраняется."""

    counts = _empty_counts()
    if not text:
        return MaskResult(text=MaskedText(""), counts=counts)

    out = text

    out, n = _sub_count(_PHONE, out, PLACEHOLDER_PHONE)
    counts[_CATEGORY_PHONE] = n

    out, n = _sub_count(_EMAIL, out, PLACEHOLDER_EMAIL)
    counts[_CATEGORY_EMAIL] = n

    out, n = _sub_count(_SNILS, out, PLACEHOLDER_ID)
    counts[_CATEGORY_ID] += n
    out, n = _sub_count(_PASSPORT, out, PLACEHOLDER_ID)
    counts[_CATEGORY_ID] += n
    out, n = _sub_count(_LONG_DIGITS, out, PLACEHOLDER_ID)
    counts[_CATEGORY_ID] += n

    out, n = _sub_count(_APARTMENT, out, PLACEHOLDER_APARTMENT)
    counts[_CATEGORY_APARTMENT] = n

    out, n = _mask_fio(out)
    counts[_CATEGORY_FIO] = n

    return MaskResult(text=MaskedText(out), counts=counts)
