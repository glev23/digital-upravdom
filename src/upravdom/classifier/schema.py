"""Схема решения классификатора (CLASSIFY-001, architecture.md §4)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.models.enums import ResponsibilityZone


class Branch(StrEnum):
    """Ветка после порога уверенности."""

    AUTO = "auto"
    CLARIFY = "clarify"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class Clarification:
    """Ответ жителя на уточняющий вопрос (второй проход)."""

    question: str
    answer: str


class LlmClassification(BaseModel):
    """Строгий JSON от LLM — валидируется портом LLM-001."""

    problem_type: str = Field(description="код типа проблемы из справочника")
    responsibility_zone: ResponsibilityZone = Field(
        description="кто отвечает: uk, rso, owner, municipality, unknown"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="уверенность 0..1")
    # Номера показанных фрагментов, а не строки ссылок: модель пересказывает
    # «п. 5 абз. 1» как «ПП РФ №491, п. 5», и сверка строк отбрасывала верные
    # основания (живой прогон CLASSIFY-001: 9 из 15 ушли в unknown).
    cited_fragments: list[int] = Field(
        default_factory=list,
        description="номера показанных фрагментов [1..N], на которые опирается решение",
    )
    reasoning: str = Field(
        default="",
        max_length=300,
        description="краткое обоснование для журнала, не для жителя",
    )
    clarifying_question: str | None = Field(
        default=None,
        description="уточняющий вопрос, если не уверены между двумя вариантами",
    )
    clarifying_options: list[str] = Field(
        default_factory=list,
        description="2–4 коротких варианта ответа для кнопок",
    )

    @field_validator("reasoning")
    @classmethod
    def _trim_reasoning(cls, value: str) -> str:
        return value.strip()[:300]


@dataclass(slots=True, frozen=True)
class ClassificationResult:
    """Решение сервиса classify() для FLOW-001."""

    problem_type: str
    responsibility_zone: ResponsibilityZone
    confidence: float
    branch: Branch
    citations: list[RetrievedChunk] = field(default_factory=list)
    clarifying_question: str | None = None
    clarifying_options: list[str] = field(default_factory=list)
    fallback_used: bool = False
    log_id: uuid.UUID | None = None
    model_name: str | None = None
