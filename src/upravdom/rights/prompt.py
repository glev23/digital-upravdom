"""Промпт ответа о правах (RIGHTS-001, architecture.md §6.4).

Своя версия промпта, отдельная от классификатора: у задач разный выход и
разные риски. Здесь главный риск — «уверенная выдумка»: житель пойдёт с
несуществующим пунктом в УК или ГЖИ, и ответ «не знаю» для него дешевле.
"""

from __future__ import annotations

from upravdom.classifier.llm import ChatMessage
from upravdom.knowledge.retrieval import RetrievedChunk
from upravdom.masking import MaskedText

PROMPT_VERSION = "rights-1"

_SYSTEM = """Ты справочный помощник по жилищным нормативам в боте «Цифровой Управдом».
Отвечаешь жителю многоквартирного дома на вопрос о его правах и обязанностях
организаций — строго JSON по схеме.

Железные правила:
1. Отвечай ТОЛЬКО по показанным ниже фрагментам нормативов. Никаких знаний
   «из памяти»: если ответа во фрагментах нет — sufficient = false и пустой answer.
2. В cited_fragments укажи номера [1..N] тех фрагментов, на которых
   действительно построен ответ. Пустой список означает, что ответа нет.
3. В тексте answer НЕ указывай номера актов и пунктов («ПП №354», «п. 31») —
   ссылки подставит код по cited_fragments. Пиши простым языком, 1–3
   предложения, без канцелярита.
4. Не давай советов «на всякий случай» и не додумывай условия, которых нет во
   фрагментах.
5. Не пересказывай вопрос жителя дословно.
"""


def build_messages(masked: MaskedText, *, chunks: list[RetrievedChunk]) -> list[ChatMessage]:
    """Собрать system+user. Вход — только `MaskedText` (mypy ловит сырой `str`)."""

    fragments = [f"[{i}] {chunk.label}\n{chunk.body}" for i, chunk in enumerate(chunks, start=1)]
    corpus = "\n\n".join(fragments) if fragments else "(фрагменты не найдены)"

    user = "\n".join(
        [
            "Фрагменты нормативов:",
            corpus,
            "",
            f"Вопрос жителя (замаскирован): {masked}",
        ]
    )
    return [ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=user)]
