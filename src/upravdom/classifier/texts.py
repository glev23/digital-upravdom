"""Шаблонные уточняющие вопросы без LLM (CLASSIFY-002)."""

from __future__ import annotations

# Краткие подписи типов для кнопок и вопроса.
TYPE_LABELS: dict[str, str] = {
    "cold_water": "холодную воду",
    "hot_water": "горячую воду",
    "heating": "отопление",
    "sewage": "канализацию",
    "electricity": "электричество",
    "gas": "газ",
    "elevator": "лифт",
    "roof_leak": "протечку кровли",
    "common_area": "подъезд / места общего пользования",
    "yard": "двор / придомовую территорию",
    "other": "другое",
}


def clarifying_between_types(type_a: str, type_b: str) -> tuple[str, list[str]]:
    """Вопрос выбора между двумя лучшими типами + 2–3 варианта кнопок."""

    la = TYPE_LABELS.get(type_a, type_a)
    lb = TYPE_LABELS.get(type_b, type_b)
    question = f"Это про {la} или про {lb}?"
    options = [f"Про {la}", f"Про {lb}", "Не уверен / другое"]
    return question, options
