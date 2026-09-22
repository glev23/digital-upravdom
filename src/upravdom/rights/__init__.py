"""Публичный API модуля справки по правам (RIGHTS-001)."""

from __future__ import annotations

from upravdom.rights.detect import RightsQuestion, detect
from upravdom.rights.service import RightsAnswer, answer_question

__all__ = ["RightsAnswer", "RightsQuestion", "answer_question", "detect"]
