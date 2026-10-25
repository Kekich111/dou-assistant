"""Уточняющие вопросы для неоднозначных запросов (без LLM)."""
from __future__ import annotations

import re

# Варианты и сроки взяты из п. 10.3 Инструкции (максимальный срок рассмотрения).
# Аналитику: сверить список и состав «нормативных» видов с главой 3.
NORMATIVE_OPTIONS = [
    {"label": "Положение", "max_days": 10},
    {"label": "Регламент", "max_days": 10},
    {"label": "Порядок", "max_days": 10},
    {"label": "Правила", "max_days": 10},
    {"label": "Иной документ", "max_days": 5},
]

POINT_REFS = ["10.2", "10.3", "10.11"]

TYPE_STEMS = ("положени", "регламент", "правила", "правил")


def _tokens(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]+", text.lower())


def _has(tokens: list[str], stems: tuple[str, ...]) -> bool:
    return any(t.startswith(stems) for t in tokens)


def clarify_block(question: str) -> dict | None:
    t = _tokens(question)
    if not _has(t, ("нормативн", "локальн")):
        return None
    if not _has(t, ("согласов", "визиров", "срок")):
        return None
    if _has(t, TYPE_STEMS):          # вид документа уже назван
        return None
    return {
        "id": "normative_doc_type",
        "intro": (
            "Порядок и срок согласования зависят от вида документа "
            "(срок по каждому виду указан в п. 10.3), поэтому сначала уточните вид."
        ),
        "question": "Какой вид документа вы согласовываете?",
        "options": [
            {
                **o,
                "followup_message": f"Какой порядок и сроки согласования проекта документа «{o['label']}»?",
            }
            for o in NORMATIVE_OPTIONS
        ],
        "point_refs": POINT_REFS,
    }