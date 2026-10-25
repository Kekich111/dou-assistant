"""Уточняющие вопросы для неоднозначных запросов (без LLM)."""
from __future__ import annotations

import re

# Варианты и сроки взяты из п. 10.3 Инструкции (максимальный срок рассмотрения).
NORMATIVE_OPTIONS = [
    {"label": "Положение", "max_days": 10},
    {"label": "Регламент", "max_days": 10},
    {"label": "Порядок", "max_days": 10},
    {"label": "Правила", "max_days": 10},
    {"label": "Иной документ", "max_days": 5},
]
KIND_OPTIONS = [
    {"label": "Приказ", "max_days": 10},
    {"label": "Распоряжение", "max_days": 3},
]

POINT_REFS = ["10.2", "10.3", "10.11"]

TYPE_STEMS = ("положени", "регламент", "правил")
# если назван любой вид документа, вопрос уже конкретный
KIND_STEMS = TYPE_STEMS + ("приказ", "распоряж", "служебн", "письм", "докладн",
                           "протокол", "заявк", "договор")

# «просто приказ» / «нужно распоряжение»: спрашиваем, какой именно
BARE_FORMS = {"приказ": "приказ", "приказа": "приказ",
              "распоряжение": "распоряжение", "распоряжения": "распоряжение"}
FILLER = {"нужен", "нужно", "нужна", "надо", "хочу", "мне", "составить", "создать",
          "сделать", "подготовить", "пожалуйста", "а", "и"} | set(BARE_FORMS)

BARE_OPTIONS = {
    "приказ": [
        ("Ввод ИС в эксплуатацию", "Нужен приказ о вводе ИС в эксплуатацию"),
        ("Ответственные за делопроизводство", "Нужен приказ о назначении ответственных за делопроизводство"),
        ("Приём и передача дел и имущества", "Нужен приказ о приёме и передаче дел и имущества"),
        ("Утверждение локального акта", "Нужен приказ об утверждении локального акта"),
    ],
    "распоряжение": [
        ("Аудит процессов", "Нужно распоряжение о проведении аудита процессов"),
        ("Проектный комитет", "Нужно распоряжение о создании проектного комитета"),
        ("Рабочая группа", "Нужно распоряжение о создании рабочей группы"),
    ],
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]+", text.lower())


def _has(tokens: list[str], stems: tuple[str, ...]) -> bool:
    return any(t.startswith(stems) for t in tokens)


def _bare_kind(t: list[str]) -> str | None:
    if not t or any(w not in FILLER for w in t):
        return None
    kinds = {BARE_FORMS[w] for w in t if w in BARE_FORMS}
    return kinds.pop() if len(kinds) == 1 else None


def _type_block(options: list[dict], force: bool) -> dict:
    return {
        "id": "normative_doc_type",
        "intro": (
            "Порядок и срок согласования зависят от вида документа "
            "(срок по каждому виду указан в п. 10.3), поэтому сначала уточните вид."
        ),
        "question": "Какой вид документа вы согласовываете?",
        "options": [
            {**o, "followup_message": f"Какой порядок и сроки согласования проекта документа «{o['label']}»?"}
            for o in options
        ],
        "point_refs": POINT_REFS,
        "force": force,
    }


def clarify_block(question: str) -> dict | None:
    t = _tokens(question)

    kind = _bare_kind(t)
    if kind:
        return {
            "id": "bare_kind",
            "intro": ("У каждого вида документа свой модуль-шаблон и свой маршрут визирования, "
                      "поэтому сначала уточните, какой именно документ нужен."),
            "question": "Какой документ нужен?",
            "options": [{"label": a, "followup_message": b} for a, b in BARE_OPTIONS[kind]],
            "point_refs": [],
            "force": True,
            "no_sources": True,
        }

    # нормативный документ без названного вида (как раньше)
    if _has(t, ("нормативн", "локальн")) and _has(t, ("согласов", "визиров", "срок")) \
            and not _has(t, TYPE_STEMS):
        return _type_block(NORMATIVE_OPTIONS, force=False)

    # «сколько дней на согласование» без вида документа
    if _has(t, ("согласов", "визиров")) and _has(t, ("срок", "дн", "скок", "сколь", "долго")) \
            and not _has(t, KIND_STEMS):
        return _type_block(KIND_OPTIONS + NORMATIVE_OPTIONS, force=True)

    return None