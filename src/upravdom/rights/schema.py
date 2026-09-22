"""Схема ответа о правах (RIGHTS-001, architecture.md §6.4)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class LlmRightsAnswer(BaseModel):
    """Строгий JSON от LLM — валидируется портом LLM-001."""

    sufficient: bool = Field(description="false, если в показанных фрагментах нет ответа на вопрос")
    answer: str = Field(
        default="",
        max_length=700,
        description="ответ простым языком, без номеров актов и пунктов — ссылки добавит код",
    )
    # Номера показанных фрагментов, а не строки ссылок: тот же приём, что в
    # CLASSIFY-001 (`confirm_citations`) — модель пересказывает ссылку своими
    # словами, и сверка строк отбрасывала бы верные основания.
    cited_fragments: list[int] = Field(
        default_factory=list,
        description="номера показанных фрагментов [1..N], на которые опирается ответ",
    )

    @field_validator("answer")
    @classmethod
    def _trim_answer(cls, value: str) -> str:
        return value.strip()[:700]
