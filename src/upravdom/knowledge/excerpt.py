"""Короткая цитата пункта нормы для ответа жителю.

Пункты длинные (ПП №491 п. 2 — ~2000 символов, ПП №416 п. 14 — одно
предложение на ~950). Прежнее окно в 340–480 символов вокруг совпадения
читалось как стена юридического текста и обрывалось с «…» с обеих сторон:
живой прогон 28.09.2026 (заявки № 18–20) показал перечисление «б) крыши;
в) ограждающие несущие конструкции…», не относящееся к проблеме жителя, и
пересказ нормы, которая двумя строками выше уже написана по-человечески.

Теперь в сообщении одна короткая мысль, а полный пункт житель открывает
кнопкой «Показать норму» (UX-правка 29.09.2026).
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
# Совпадение по началу слова: «лифт» находит «лифты», «лифтовые».
_STEM_LEN = 4
_LEAD_MAX = 150
_CLAUSE_NO = re.compile(r"^\d+(\.\d+)*\.\s*")
_QUOTE_LIMIT = 180
# Подпункт перечня: «а) помещения…; б) крыши; в) …».
_LIST_ITEM = re.compile(r"(?:^|\s)[а-яё]\)\s", re.IGNORECASE)
# Точка после этих слов — сокращение, а не граница фразы: «п. 5», «ст. 161».
_ABBREV = frozenset(
    "п пп ст ч чч абз подп пт гл разд прил г гг в вв руб тыс млн др см напр т е и".split()
)
# Обрыв на служебном слове читается как оборванная мысль: «…в течение не…».
_DANGLING = frozenset(
    "не и а но или в во с со на по для от до при из за над под о об у ни же бы что как".split()
)


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


def _is_boundary(text: str, i: int) -> bool:
    """Граница фразы: запятая, точка с запятой или точка не внутри сокращения."""

    if text[i] in ";,":
        return True
    words = _WORD.findall(text[:i])
    return not (words and (_norm(words[-1]) in _ABBREV or words[-1].isdigit()))


def _drop_dangling(text: str) -> str:
    words = text.split()
    while words and _norm(words[-1].strip(".,;:()")) in _DANGLING:
        words.pop()
    return " ".join(words).strip(" ,;:")


def _opening(text: str, limit: int) -> str:
    """Начало пункта до границы фразы, «…» только в конце и только при обрыве."""

    if len(text) <= limit:
        return text
    best = 0
    for i, ch in enumerate(text[:limit]):
        if ch in ".;," and _is_boundary(text, i):
            best = i
    if best == 0:
        space = text.rfind(" ", 0, limit)
        best = space if space > 0 else limit
    head = _drop_dangling(text[:best].strip(" ,;:"))
    return f"{head}…" if head else ""


def _list_item(text: str, pos: int, limit: int) -> str | None:
    """«Вводная: … подпункт», если совпадение попало в короткий подпункт перечня.

    Для кровли это «В состав общего имущества включаются: … б) крыши» вместо
    начала пункта про помещения вообще. Пропуск остальных подпунктов помечен
    «…» — это не обрыв, а честно обозначенное сокращение цитаты.
    """

    colon = text.find(":")
    if not 0 < colon < _LEAD_MAX or pos <= colon:
        return None
    starts = [m.start() for m in _LIST_ITEM.finditer(text, colon) if m.start() <= pos]
    if not starts:
        return None
    end = text.find(";", pos)
    item = text[starts[-1] : end if end != -1 else len(text)].strip(" ,;:.")
    quote = f"{text[: colon + 1]} … {item}"
    return quote if len(quote) <= limit else None


def norm_quote(body: str, *queries: str, limit: int = _QUOTE_LIMIT) -> str:
    """Короткая цитата пункта для сообщения жителю.

    Совпадение в коротком подпункте перечня — «вводная: … подпункт». Иначе —
    начало пункта: в нормативе там его предмет («В состав общего имущества
    включаются внутридомовые инженерные системы холодного и горячего
    водоснабжения…»), то есть ровно то, что объясняет зону ответственности.

    Законченной фразы целиком не требуем: цитируемые пункты базы — одно
    предложение на 500–950 символов, такое правило убрало бы цитату почти
    отовсюду. Номер пункта не повторяется — он уже есть в подписи ссылки.
    """

    text = _CLAUSE_NO.sub("", " ".join(body.split())).strip()
    if not text:
        return ""
    pos = match_position(text, *queries)
    if pos is not None:
        item = _list_item(text, pos, limit)
        if item:
            return item
    return _opening(text, limit)
