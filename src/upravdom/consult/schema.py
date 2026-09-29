"""Схема ответа консультанта."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class LlmConsultAnswer(BaseModel):
    """Строгий JSON от LLM — валидируется портом LLM-001."""

    needs_ticket: bool = Field(
        default=False,
        description="true — ситуацию должна исправить управляющая компания (уборка, поломка)",
    )
    answer: str = Field(
        default="",
        max_length=600,
        description="совет жителю простым языком, 2–4 предложения, без номеров актов и телефонов",
    )
    cited_fragments: list[int] = Field(
        default_factory=list,
        description="номера показанных фрагментов [1..N], если совет на них опирается",
    )

    @field_validator("answer")
    @classmethod
    def _trim_answer(cls, value: str) -> str:
        return value.strip()[:600]
