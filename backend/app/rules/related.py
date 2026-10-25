"""Связанные пункты Инструкции, которые подтягиваются по теме вопроса (без LLM)."""
from __future__ import annotations

import re

# Правило срабатывает, если есть основа из группы A и основа из группы B.
RULES = [
    {
        "a": ("служебн", "служебк"),
        "b": ("записк", "письм", "переписк", "сопроводит"),
        "points": ["9.9.2"],
    },
]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]+", text.lower())


def _has(tokens: list[str], stems: tuple[str, ...]) -> bool:
    return any(t.startswith(stems) for t in tokens)


def extra_points(question: str) -> list[str]:
    t = _tokens(question)
    result: list[str] = []
    for rule in RULES:
        if _has(t, rule["a"]) and _has(t, rule["b"]):
            result.extend(p for p in rule["points"] if p not in result)
    return result