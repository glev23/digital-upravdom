"""Промпт классификации (CLASSIFY-001).

PROMPT_VERSION пишется в classification_logs и участвует в инвалидации
кэша (architecture.md §6.3). Few-shot только сочинённые — не из EVAL-001.
"""

from __future__ import annotations

from dataclasses import dataclass

from upravdom.classifier.llm.port import ChatMessage
from upravdom.classifier.schema import Clarification
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.masking import MaskedText
from upravdom.models.enums import ResponsibilityZone

# 2 — ссылки на фрагменты по номеру вместо строки ref.
PROMPT_VERSION = "2"

# Сочинённые примеры. Помечены явно — не брать фразы из EVAL-001.
_SYNTHETIC_FEW_SHOT: tuple[tuple[str, str], ...] = (
    (
        "[сочинённый пример] В подъезде на третьем этаже три дня не горит лампа.",
        (
            "problem_type=common_area, responsibility_zone=uk, "
            "cited_fragments — номера показанных фрагментов про общее имущество."
        ),
    ),
    (
        "[сочинённый пример] После вентиля под раковиной капает — лужа на полу "
        "только в моей квартире.",
        (
            "problem_type=cold_water или sewage по смыслу, "
            "responsibility_zone=owner (после первого отключающего устройства)."
        ),
    ),
)


@dataclass(slots=True, frozen=True)
class ProblemTypeInfo:
    code: str
    title: str
    default_responsibility_zone: ResponsibilityZone
    norm_reference: str


def build_messages(
    masked: MaskedText,
    *,
    chunks: list[RetrievedChunk],
    problem_types: list[ProblemTypeInfo],
    clarification: Clarification | None = None,
) -> list[ChatMessage]:
    """Собрать system+user. Вход — только MaskedText (mypy ловит сырой str)."""

    taxonomy = "\n".join(
        (
            f"- `{pt.code}` — {pt.title}. "
            f"Зона по умолчанию: {pt.default_responsibility_zone.value}. "
            f"{pt.norm_reference}"
        )
        for pt in problem_types
    )
    codes = ", ".join(pt.code for pt in problem_types)

    fragments: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        fragments.append(f"[{i}] {chunk.label}\n{chunk.body}")
    corpus = "\n\n".join(fragments) if fragments else "(фрагменты не найдены)"

    few_shot = "\n".join(
        f"Пример {i}: обращение «{text}» → {hint}"
        for i, (text, hint) in enumerate(_SYNTHETIC_FEW_SHOT, start=1)
    )

    system = f"""Ты классификатор обращений жителей МКД для бота «Цифровой Управдом».
Задача: по замаскированному тексту и найденным фрагментам нормативов вернуть
строго JSON по схеме. Не выдумывай пункты норм: в cited_fragments укажи
номера [1..N] показанных фрагментов, на которые опирается решение.
reasoning ≤ 300 символов, не цитируй обращение дословно.

Допустимые problem_type: {codes}.

Таксономия:
{taxonomy}

Правила зон ответственности (опирайся на нормы в фрагментах):
- граница «первое отключающее устройство / стык» (ПП №491, п. 5): до него —
  общее имущество (часто uk), после него внутри квартиры — owner;
- внешние сети до внешней стены дома — rso (ПП №491, п. 8);
- территория за границей земельного участка МКД — municipality;
- если нельзя определить — unknown.

Если не уверен между двумя вариантами (зона или тип), задай clarifying_question
и 2–4 коротких clarifying_options для кнопок. Иначе clarifying_question = null
и clarifying_options = [].

Сочинённые ориентиры (не реальные обращения):
{few_shot}
"""

    user_parts = [
        "Найденные фрагменты нормативов:",
        corpus,
        "",
        f"Обращение жителя (замаскировано): {masked}",
    ]
    if clarification is not None:
        user_parts.extend(
            [
                "",
                f"Ранее заданный уточняющий вопрос: {clarification.question}",
                f"Ответ жителя: {clarification.answer}",
                "Это второй проход — новый clarifying_question не задавай.",
            ]
        )
    user_parts.append("Ответь строго по JSON-схеме.")

    return [
        ChatMessage(role="system", content=system.strip()),
        ChatMessage(role="user", content="\n".join(user_parts)),
    ]
