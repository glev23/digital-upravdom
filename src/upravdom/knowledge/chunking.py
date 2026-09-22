"""Разбор исходников нормативов и чанкинг по пунктам (KB-001)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from upravdom.embeddings import PROMPT_SEARCH_DOCUMENT, token_length

CHUNKER_VERSION = "3"

_UNIT_HEADER = re.compile(r"^##\s+(?P<label>.+?)\s*$", re.MULTILINE)
# Конец предложения — после буквы/скобки/кавычки и перед заглавной: «2. В состав…»
# и «п. 5» не режутся (номер пункта отдельным фрагментом был бы мусором).
_SENTENCE_END = re.compile(r"(?<=[а-яёa-z)»\"][.!?…])\s+(?=[А-ЯЁA-Z])")
_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass(slots=True, frozen=True)
class SourceMeta:
    source_key: str
    title: str
    version: str
    effective_from: date
    effective_to: date | None
    region_code: str | None
    source_url: str
    retrieved_at: date
    management_company_id: str | None = None


@dataclass(slots=True, frozen=True)
class RawChunk:
    chunk_text: str
    chunk_meta: dict[str, Any]
    document: dict[str, Any]


def _parse_date(value: str | None) -> date | None:
    if value is None or value in {"", "null", "~"}:
        return None
    return date.fromisoformat(value.strip().strip("\"'"))


def _parse_front_matter(fm: str) -> dict[str, str | None]:
    """Минимальный YAML: `key: value` по строкам, без вложенности."""

    data: dict[str, str | None] = {}
    for line in fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        value = raw.strip()
        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            value = value[1:-1]
        data[key.strip()] = None if value in {"", "null", "~"} else value
    return data


def parse_source(path: Path) -> tuple[SourceMeta, str]:
    raw = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(raw)
    if not match:
        msg = f"{path}: ожидается YAML front-matter"
        raise ValueError(msg)
    data = _parse_front_matter(match.group(1))
    meta = SourceMeta(
        source_key=str(data["source_key"]),
        title=str(data["title"]),
        version=str(data["version"]),
        effective_from=_parse_date(data.get("effective_from")) or date.min,
        effective_to=_parse_date(data.get("effective_to")),
        region_code=data.get("region_code"),
        source_url=str(data["source_url"]),
        retrieved_at=_parse_date(data.get("retrieved_at")) or date.today(),
        management_company_id=data.get("management_company_id"),
    )
    return meta, match.group(2).strip()


def _short_title(meta: SourceMeta) -> str:
    mapping = {
        "pp491": "ПП РФ №491",
        "pp354": "ПП РФ №354",
        "pp416": "ПП РФ №416",
        "pp626": "ПП РФ №626",
        # ПП №40 не самостоятельные правила, а новая редакция п. 27, 30 ПП №416.
        "pp40": "ПП РФ №416 (в ред. ПП РФ №40)",
        "pp290": "ПП РФ №290",
        "zhk": "ЖК РФ",
    }
    return mapping.get(meta.source_key, meta.title)


def _ref_from_label(label: str) -> str:
    return " ".join(label.split())


def _parse_ref_parts(label: str) -> dict[str, str | None]:
    article = point = subpoint = None
    m = re.search(r"ст\.\s*(\d+)", label, re.I)
    if m:
        article = m.group(1)
    m_part = re.search(r"ч\.\s*(\d+(?:\.\d+)?)", label, re.I)
    if m and m_part:
        article = f"{m.group(1)}.{m_part.group(1)}"
    m = re.search(r"(?:п\.|пункт)\s*(\d+[a-zа-я]?)", label, re.I)
    if m:
        point = m.group(1)
    m = re.search(r"(?:пп\.|подп\.|подпункт)\s*([а-я](?:\s?\d)?|[\d.]+)", label, re.I)
    if m:
        subpoint = m.group(1)
    return {"article": article, "point": point, "subpoint": subpoint}


def _fits(text: str) -> bool:
    return token_length(text, prompt=PROMPT_SEARCH_DOCUMENT) <= 512


def _join(context: str, lines: list[str]) -> str:
    return "\n".join([context, *lines])


def _hard_split(context: str, text: str) -> list[str]:
    """Режет по словам так, чтобы каждый кусок вместе с заголовком укладывался в лимит."""

    parts: list[str] = []
    buf: list[str] = []
    for word in text.split():
        candidate = " ".join([*buf, word])
        if buf and not _fits(_join(context, [candidate])):
            parts.append(" ".join(buf))
            buf = [word]
        else:
            buf.append(word)
    if buf:
        parts.append(" ".join(buf))
    return parts


def _units(context: str, body: str) -> list[str]:
    """Строки пункта (в исходниках подпункт — одна строка); слишком длинная строка
    делится по предложениям, затем по словам — всегда с учётом заголовка."""

    units: list[str] = []
    for line in (ln.strip() for ln in body.splitlines()):
        if not line:
            continue
        if _fits(_join(context, [line])):
            units.append(line)
            continue
        for sentence in (x.strip() for x in _SENTENCE_END.split(line)):
            if not sentence:
                continue
            if _fits(_join(context, [sentence])):
                units.append(sentence)
            else:
                units.extend(_hard_split(context, sentence))
    return units


def _pieces(context: str, body: str) -> list[str]:
    """Тексты чанков пункта, каждый — с заголовком-ссылкой.

    Длинный пункт режется по строкам; у продолжений повторяется вводная строка
    («2. В состав общего имущества включаются:») — без неё подпункт «ж)» теряет,
    к чему относится. Это вместо перекрытия «последним предложением».
    """

    whole = _join(context, [body.strip()])
    if _fits(whole):
        return [whole]

    units = _units(context, body)
    lead = units[0] if len(units) > 1 and units[0].endswith(":") else None
    rest = units[1:] if lead else units
    groups: list[list[str]] = []
    buf: list[str] = [lead] if lead else []
    for unit in rest:
        if _fits(_join(context, [*buf, unit])):
            buf.append(unit)
            continue
        if buf and buf != [lead]:
            groups.append(buf)
        buf = [lead, unit] if lead and _fits(_join(context, [lead, unit])) else [unit]
    if buf and buf != [lead]:
        groups.append(buf)
    return [_join(context, g) for g in groups]


def chunk_source(meta: SourceMeta, body: str) -> list[RawChunk]:
    matches = list(_UNIT_HEADER.finditer(body))
    if not matches:
        msg = f"{meta.source_key}: нет заголовков ## единиц (п./ст.)"
        raise ValueError(msg)

    short = _short_title(meta)
    document = {
        "source_key": meta.source_key,
        "title": meta.title,
        "version": meta.version,
        "effective_from": meta.effective_from.isoformat(),
        "effective_to": meta.effective_to.isoformat() if meta.effective_to else None,
        "region_code": meta.region_code,
        "source_url": meta.source_url,
        "retrieved_at": meta.retrieved_at.isoformat(),
        "management_company_id": meta.management_company_id,
    }

    chunks: list[RawChunk] = []
    for i, match in enumerate(matches):
        label = match.group("label").strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        unit_body = body[start:end].strip()
        if not unit_body:
            continue
        ref = _ref_from_label(label)
        context = f"{short}, {ref}"
        ref_parts = _parse_ref_parts(label)
        pieces = _pieces(context, unit_body)
        multi = len(pieces) > 1
        # Сквозной номер части: из него строится chunk_id, совпадений быть не должно.
        for part, text in enumerate(pieces):
            chunks.append(
                RawChunk(
                    chunk_text=text,
                    chunk_meta={
                        "source_key": meta.source_key,
                        **ref_parts,
                        "title": short,
                        "ref": f"{ref} ({part + 1})" if multi else ref,
                        "part": part,
                    },
                    document=document,
                )
            )
    return chunks


def chunk_sources_dir(sources_dir: Path) -> list[RawChunk]:
    paths = sorted(sources_dir.glob("*.md"))
    if not paths:
        msg = f"нет исходников в {sources_dir}"
        raise FileNotFoundError(msg)
    result: list[RawChunk] = []
    for path in paths:
        meta, body = parse_source(path)
        result.extend(chunk_source(meta, body))
    return result
