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
# 200 символов обрывали пункт посреди фразы («…за оказание всех услуг и (или)
# выполнение работ…»): обрывок читался как незаконченная мысль и на живом
# прогоне противоречил ответу «отвечает УК». Окно шире и режется по границе
# фразы, а не по произвольному слову.
_LIMIT = 340
# Граница фразы ищется в хвосте окна и чуть за его пределами: у длинного
# пункта (ЖК РФ, ст. 161 ч. 2.3 — одно предложение на 1300 символов)
# ближайшая запятая назад отрезала бы половину окна.
_CLAUSE_TAIL = 0.55
_CLAUSE_SLACK = 0.5
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


def _clause_end(text: str, start: int, end: int, *, limit: int) -> int:
    """Конец окна по границе фразы: точка → точка с запятой → запятая → слово.

    Для каждого знака берётся ближайшая к желаемому концу граница — назад по
    хвосту окна или вперёд в пределах допуска.
    """

    floor = start + int((end - start) * _CLAUSE_TAIL)
    ceiling = min(len(text), end + int(limit * _CLAUSE_SLACK))
    for mark in (".", ";", ","):
        back = text.rfind(mark, floor, end)
        forward = text.find(mark, end, ceiling)
        best = back if back > start else None
        if forward != -1 and (best is None or forward - end < end - best):
            best = forward
        if best is not None:
            return best + 1
    space = text.rfind(" ", start, end)
    return space if space > start else end


def norm_excerpt(body: str, *queries: str, limit: int = _LIMIT) -> str:
    """Окно пункта вокруг первого совпадения с запросами, по границе фразы.

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
        end = _clause_end(text, start, end, limit=limit)

    window = text[start:end].strip(" ,;:")
    if start == 0:
        # Номер пункта уже есть в подписи ссылки: «ст. 161 ч. 2.3 — 2.3. При…».
        window = _CLAUSE_NO.sub("", window)
    head = ""
    if start > 0:
        head = f"{lead_shown} … " if lead_shown and start > len(lead) else "…"
    tail = "…" if end < len(text) else ""
    return f"{head}{window}{tail}"
