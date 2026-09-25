"""Вырезка из пункта нормы для ответа жителю.

Пункты длинные (ПП №491 п. 2 — ~2000 символов), а нужное жителю слово часто
в середине: для лифта это «лифты» внутри подпункта «а». Первые N символов
пункта показывали бы вводную часть, обрезанную посреди слова.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
# Совпадение по началу слова: «лифт» находит «лифты», «лифтовые».
_STEM_LEN = 4
_LEAD_MAX = 150
_CLAUSE_NO = re.compile(r"^\d+(\.\d+)*\.\s*")


def _norm(word: str) -> str:
    return word.lower().replace("ё", "е")


def _stems(text: str) -> set[str]:
    return {_norm(w)[:_STEM_LEN] for w in _WORD.findall(text) if len(w) >= _STEM_LEN}


def _first_match(text: str, stems: set[str]) -> int | None:
    if not stems:
        return None
    for m in _WORD.finditer(text):
        w = _norm(m.group())
        if len(w) >= _STEM_LEN and w[:_STEM_LEN] in stems:
            return m.start()
    return None


def match_position(body: str, *queries: str) -> int | None:
    """Позиция первого совпадения; запросы — по убыванию приоритета."""

    text = " ".join(body.split())
    for query in queries:
        pos = _first_match(text, _stems(query))
        if pos is not None:
            return pos
    return None


def norm_excerpt(body: str, *queries: str, limit: int = 200) -> str:
    """Окно пункта вокруг первого совпадения с запросами, по границам слов.

    Запросы идут по убыванию приоритета (название типа проблемы точнее слов
    жалобы: «двери не открываются» у застрявшего лифта не должно уводить в
    подпункт про двери подъезда). Без совпадения — начало пункта.
    """

    text = " ".join(body.split())
    if not text:
        return ""
    pos = match_position(text, *queries)

    lead = ""
    colon = text.find(":")
    if 0 < colon < _LEAD_MAX:
        lead = text[: colon + 1]
    # Номер пункта («2.») уже есть в подписи ссылки — во вводной фразе он лишний.
    lead_shown = _CLAUSE_NO.sub("", lead)

    if pos is None or pos <= len(lead) + limit // 2:
        start = 0
    else:
        start = max(len(lead), pos - limit // 3)
        space = text.find(" ", start, pos + 1)
        if space != -1:
            start = space + 1

    end = min(len(text), start + limit)
    if end < len(text):
        space = text.rfind(" ", start, end)
        if space > start:
            end = space

    window = text[start:end].strip(" ,;:")
    head = ""
    if start > 0:
        head = f"{lead_shown} … " if lead_shown and start > len(lead) else "…"
    tail = "…" if end < len(text) else ""
    return f"{head}{window}{tail}"
