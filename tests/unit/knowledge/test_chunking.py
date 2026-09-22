"""Чанкинг нормативов: нумерация, лимит токенов, заголовок-контекст."""

from __future__ import annotations

from datetime import date

from upravdom.embeddings import PROMPT_SEARCH_DOCUMENT, token_length
from upravdom.knowledge.artifacts import SOURCES_DIR
from upravdom.knowledge.chunking import SourceMeta, chunk_source, chunk_sources_dir, parse_source


def test_all_source_chunks_fit_model_limit() -> None:
    chunks = chunk_sources_dir(SOURCES_DIR)
    assert chunks
    for ch in chunks:
        assert token_length(ch.chunk_text, prompt=PROMPT_SEARCH_DOCUMENT) <= 512, ch.chunk_meta


def test_chunk_starts_with_context_header() -> None:
    meta, body = parse_source(SOURCES_DIR / "pp491.md")
    chunks = chunk_source(meta, body)
    assert any(c.chunk_text.startswith("ПП РФ №491, п. 5") for c in chunks)


def test_points_are_not_merged_across_numbers() -> None:
    meta = SourceMeta(
        source_key="pp491",
        title="t",
        version="v",
        effective_from=date(2006, 8, 13),
        effective_to=None,
        region_code=None,
        source_url="https://example.test",
        retrieved_at=date(2026, 9, 22),
    )
    body = "## п. 4\nтекст про холодную воду.\n\n## п. 5\nтекст про горячую воду.\n"
    chunks = chunk_source(meta, body)
    assert len(chunks) == 2
    assert chunks[0].chunk_meta["ref"] == "п. 4"
    assert chunks[1].chunk_meta["ref"] == "п. 5"
    assert "горяч" not in chunks[0].chunk_text


def test_long_point_is_split_but_keeps_ref() -> None:
    meta = SourceMeta(
        source_key="pp491",
        title="t",
        version="v",
        effective_from=date(2006, 8, 13),
        effective_to=None,
        region_code=None,
        source_url="https://example.test",
        retrieved_at=date(2026, 9, 22),
    )
    # ~600+ токенов: повторяющиеся слова без знаков препинания
    body_text = " ".join(["водоснабжение"] * 400)
    assert token_length(f"ПП РФ №491, п. 99\n{body_text}", prompt=PROMPT_SEARCH_DOCUMENT) > 512
    chunks = chunk_source(meta, f"## п. 99\n{body_text}")
    assert len(chunks) > 1
    assert all("п. 99" in c.chunk_meta["ref"] for c in chunks)
    assert all(token_length(c.chunk_text, prompt=PROMPT_SEARCH_DOCUMENT) <= 512 for c in chunks)


def test_must_have_sources_present() -> None:
    keys = {p.stem for p in SOURCES_DIR.glob("*.md")}
    assert {"pp491", "pp354", "pp416", "zhk"} <= keys
