"""Распознавание вида документа и маршрут виз по матрице (без LLM)."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

MATRIX_PATH = Path(__file__).with_name("visa_matrix.yaml")

# Подписи подразделений: поправьте под реальные названия из Инструкции
DEPT_LABELS = {
    "legal": "Юридическое подразделение",
    "doc_dept": "Подразделение документационного обеспечения",
    "hr": "Подразделение по работе с персоналом",
}

DOC_WORDS = ("приказ", "распоряж")  # без этих слов блок виз не показываем

QUESTION = "Заполните документ самостоятельно или вместе со мной (я задам уточняющие вопросы и подготовлю готовый документ)?"


@lru_cache(maxsize=1)
def load_matrix() -> dict:
    with MATRIX_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"yes", "да", "true"}
    return bool(v)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]+", text.lower())


def _kw_matches(keyword: str, tokens: list[str]) -> bool:
    """Все значимые слова ключа должны найтись в вопросе (по префиксу основы)."""
    words = [w for w in _tokens(keyword)]
    if len(words) == 1 and len(words[0]) <= 3:      # «ис» — только целым словом
        return words[0] in tokens
    words = [w for w in words if len(w) > 2]
    if not words:
        return False
    for w in words:
        stem = w if len(w) <= 4 else w[: max(4, len(w) - 2)]
        if not any(t.startswith(stem) for t in tokens):
            return False
    return True


def detect_row(question: str) -> dict | None:
    tokens = _tokens(question)
    if not any(t.startswith(DOC_WORDS) for t in tokens):
        return None
    best, best_score = None, 0
    for row in load_matrix()["rows"]:
        if row.get("scope") != "internal":
            continue
        score = sum(len(_tokens(k)) for k in row.get("keywords", []) if _kw_matches(k, tokens))
        if score > best_score:
            best, best_score = row, score
    return best


def build_route(row: dict) -> dict:
    required = [label for key, label in DEPT_LABELS.items() if _truthy(row.get(key))]
    extra = [
        {"dept": o["dept"], "condition": o.get("condition")}  # condition=None — виза безусловная
        for o in row.get("other", [])
    ]
    modules = row.get("modules") or ([row["module"]] if row.get("module") else [])
    matrix = load_matrix()
    return {
        "row_id": row["id"],
        "document_type": row["title"],
        "required_visas": required,
        "additional_visas": extra,
        "modules": modules,
        "template_urls": [f"/api/templates/{m}" for m in modules],
        "matrix_notes": matrix.get("notes", []),
        "matrix_version": matrix.get("version"),
        "next_question": QUESTION,
    }


def visa_block(question: str) -> dict | None:
    row = detect_row(question)
    return build_route(row) if row else None

def route_for_module(module_id: str) -> dict | None:
    for row in load_matrix()["rows"]:
        mods = row.get("modules") or ([row["module"]] if row.get("module") else [])
        if module_id in mods:
            return build_route(row)
    return None

MODULE_TITLES = {
    "order_is_commissioning": "Приказ о вводе ИС в эксплуатацию",
    "order_responsible_docflow": "Приказ о назначении ответственных за делопроизводство",
    "order_handover": "Приказ о приёме и передаче дел и имущества",
    "order_local_act": "Приказ об утверждении локального акта",
    "decree_process_audit": "Распоряжение о проведении аудита процессов",
    "decree_project_committee": "Распоряжение о создании проектного комитета",
    "decree_workgroup": "Распоряжение о создании рабочей группы",
}

# Если строка матрицы содержит несколько модулей, выбираем по словам вопроса.
MODULE_HINTS = {
    "decree_workgroup": ("рабоч", "групп"),
    "decree_project_committee": ("проектн", "комитет"),
}

_base_visa_block = visa_block


def visa_block(question: str) -> dict | None:
    block = _base_visa_block(question)
    if not block:
        return block
    modules = list(block.get("modules") or [])
    if len(modules) > 1:
        t = _tokens(question)
        chosen = [
            m for m in modules
            if all(any(tok.startswith(s) for tok in t) for s in MODULE_HINTS.get(m, ("\0",)))
        ]
        if chosen:
            modules = chosen
    block["modules"] = modules
    block["template_urls"] = [f"/api/templates/{m}" for m in modules]
    block["templates"] = [
        {"module": m, "title": MODULE_TITLES.get(m, m), "url": f"/api/templates/{m}"}
        for m in modules
    ]
    return block

def ordered_route(row: dict) -> list[dict]:
    """Маршрут визирования по п. 10.6 Инструкции с подстановкой подразделений из Матрицы."""
    steps = [
        {"title": "Ответственный исполнитель", "note": "п. 10.6, подп. 1"},
        {"title": "Работники ответственного подразделения, участвовавшие в разработке", "note": "п. 10.6, подп. 2"},
        {"title": "Руководитель ответственного подразделения", "note": "п. 10.6, подп. 3"},
        {"title": "Руководители или заместители руководителей заинтересованных подразделений",
         "note": "п. 10.6, подп. 4; состав определяет исполнитель (п. 10.11)"},
    ]
    matrix = []
    if _truthy(row.get("hr")):
        matrix.append(DEPT_LABELS["hr"])
    for o in row.get("other", []) or []:
        matrix.append(o["dept"] + (f" (если: {o['condition']})" if o.get("condition") else ""))
    if matrix:
        steps.append({
            "title": "Подразделения обязательного визирования по Матрице: " + "; ".join(matrix),
            "note": "п. 10.6, подп. 5; порядок между ними Инструкцией не установлен",
        })
    if _truthy(row.get("legal")):
        steps.append({"title": "Юридическое подразделение", "note": "п. 10.6, подп. 6; обязательно по Матрице"})
    if _truthy(row.get("doc_dept")):
        steps.append({"title": "Департамент ДОУ", "note": "п. 10.6, подп. 7; обязательно по Матрице"})
    steps.append({
        "title": "Должностное лицо, координирующее работу ответственного подразделения",
        "note": "п. 10.6, подп. 8; если оно не является подписантом",
    })
    return steps


_prev_visa_block = visa_block


def visa_block(question: str) -> dict | None:
    block = _prev_visa_block(question)
    if not block:
        return block
    row = detect_row(question)
    if row:
        block["route"] = ordered_route(row)
    return block

_prev2_visa_block = visa_block


def visa_block(question: str) -> dict | None:
    block = _prev2_visa_block(question)
    if block:
        return block
    try:
        from app.rules.semantic import classify
        hit = classify(question)
    except Exception:  # noqa: BLE001 — семантика необязательна
        import logging
        logging.getLogger(__name__).exception("semantic classify failed")
        return None
    if not hit:
        return None
    row = next((r for r in load_matrix()["rows"] if r["id"] == hit["row_id"]), None)
    if not row:
        return None
    block = build_route(row)
    modules = hit["modules"]
    block["modules"] = modules
    block["template_urls"] = [f"/api/templates/{m}" for m in modules]
    block["templates"] = [
        {"module": m, "title": MODULE_TITLES.get(m, m), "url": f"/api/templates/{m}"}
        for m in modules
    ]
    block["route"] = ordered_route(row)
    block["points"] = hit["points"]
    block["detected_by"] = "semantic"
    block["confidence"] = round(hit["score"], 2)
    return block