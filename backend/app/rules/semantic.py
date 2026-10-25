"""Определение вида документа по смыслу через bge-m3 (без LLM).

Проверка порогов:
    docker compose exec -e PYTHONPATH=/app backend python -m app.rules.semantic
"""
from __future__ import annotations

import logging
import math
import os
import threading

from app.rag.retriever import EMBED_MODEL, get_retriever

log = logging.getLogger(__name__)

MIN_SCORE = float(os.getenv("DOCTYPE_MIN", "0.62"))
MIN_MARGIN = float(os.getenv("DOCTYPE_MARGIN", "0.03"))

TYPES = [
    {"id": "is", "row_id": "internal_is_orders",
     "modules": ["order_is_commissioning"], "points": ["9.5.6"],
     "examples": [
         "приказ о вводе информационной системы в эксплуатацию",
         "запуск новой системы в работу",
         "ввод инфосистемы в эксплуатацию",
         "приказ про ИС",
         "ввести программный комплекс в промышленную эксплуатацию",
     ]},
    {"id": "handover", "row_id": "internal_handover_orders",
     "modules": ["order_handover"], "points": [],
     "examples": [
         "приказ о приёме и передаче дел и имущества",
         "передача дел при увольнении сотрудника",
         "передать дела и имущество другому работнику",
         "освобождение от должности и передача дел",
         "приём-передача дел при реорганизации подразделения",
     ]},
    {"id": "docflow", "row_id": "internal_other_orders",
     "modules": ["order_responsible_docflow"], "points": [],
     "examples": [
         "приказ о назначении ответственных за делопроизводство",
         "назначить ответственного за документооборот в подразделении",
         "ответственный за ДОУ в отделе",
     ]},
    {"id": "local_act", "row_id": "internal_other_orders",
     "modules": ["order_local_act"], "points": ["9.5.1"],
     "examples": [
         "приказ об утверждении и введении в действие локального акта",
         "утвердить положение приказом",
         "утвердить регламент и ввести его в действие",
         "приказ об утверждении порядка",
         "ввести в действие внутренний документ",
     ]},
    {"id": "audit", "row_id": "internal_other_decrees",
     "modules": ["decree_process_audit"], "points": [],
     "examples": [
         "распоряжение о проведении внутреннего аудита процессов",
         "провести аудит процессов",
         "внутренний аудит бизнес-процессов",
     ]},
    {"id": "workgroup", "row_id": "internal_temp_bodies_decrees",
     "modules": ["decree_workgroup"], "points": ["9.7.1"],
     "examples": [
         "распоряжение о создании рабочей группы",
         "создать рабочую группу",
         "нужно создать группу для решения задачи",
         "временная рабочая группа",
     ]},
    {"id": "committee", "row_id": "internal_temp_bodies_decrees",
     "modules": ["decree_project_committee"], "points": ["9.7.1"],
     "examples": [
         "распоряжение о создании проектного комитета",
         "создать проектный комитет",
         "комитет по проекту",
     ]},
    {"id": "commission", "row_id": "internal_temp_bodies_decrees",
     "modules": ["decree_workgroup", "decree_project_committee"], "points": ["9.7.1"],
     "examples": [
         "создать комиссию",
         "временная комиссия",
         "хочу создать комиссию, какие визы нужны",
     ]},
]

_lock = threading.Lock()
_index: list[tuple[int, list[float]]] | None = None


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _embed(texts: list[str]) -> list[list[float]]:
    r = get_retriever()
    resp = r._http.post(
        f"{r.ollama_url}/api/embed",
        json={"model": EMBED_MODEL, "input": texts, "keep_alive": "30m"},
    )
    resp.raise_for_status()
    return resp.json()["embeddings"]


def _get_index() -> list[tuple[int, list[float]]]:
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                texts, owners = [], []
                for ti, t in enumerate(TYPES):
                    for ex in t["examples"]:
                        texts.append(ex)
                        owners.append(ti)
                vecs = [_unit(v) for v in _embed(texts)]
                _index = list(zip(owners, vecs))
    return _index


def scores(question: str) -> list[tuple[int, float]]:
    q = _unit(_embed([question])[0])
    best: dict[int, float] = {}
    for ti, v in _get_index():
        s = sum(a * b for a, b in zip(q, v))
        if s > best.get(ti, -1.0):
            best[ti] = s
    return sorted(best.items(), key=lambda x: -x[1])


def classify(question: str) -> dict | None:
    q = (question or "").strip()
    if not q or len(q) > 300:
        return None

    # страховка: вопросы про сроки, регистрацию, подписание и т. п. не считаем «созданием документа»,
    # если в них нет признаков создания
    BLOCK = ("срок", "зарегистр", "кто подпис", "подписан", "когда ", "нужно ли", "как согласов")
    CREATE = ("создат", "оформить", "оформл", "подготов", "нужен ", "нужна ", "ввод", "запуск",
              "передач", "утверд", "аудит", "комисс", "комитет", "групп")
    q_low = q.lower() + " "
    if any(b in q_low for b in BLOCK) and not any(c in q_low for c in CREATE):
        return None

    ranked = scores(q)
    if not ranked:
        return None
    top_i, top = ranked[0]
    if top < MIN_SCORE:
        return None
    close = [ti for ti, s in ranked if top - s <= MIN_MARGIN]
    if len({TYPES[ti]["row_id"] for ti in close}) > 1:
        return None  # неоднозначно: разные строки матрицы
    modules: list[str] = []
    points: list[str] = []
    for ti in close:
        for m in TYPES[ti]["modules"]:
            if m not in modules:
                modules.append(m)
        for p in TYPES[ti]["points"]:
            if p not in points:
                points.append(p)
    return {"row_id": TYPES[top_i]["row_id"], "modules": modules,
            "points": points, "score": top, "types": [TYPES[ti]["id"] for ti in close]}


if __name__ == "__main__":
    import sys

    tests = [
        "приказ про ИС",
        "нужен приказ о запуске новой системы в работу",
        "ввод инфосистемы в экплуатацию",
        "как оформить распоряжение о создании группы",
        "хочу создать комиссию, какие визы нужны",
        "приказ о передаче дел при увольнении",
        "приказ об утверждении положения",
        "распоряжение об аудите процессов",
        "--- не про создание документа ---",
        "Нужно ли сопроводительное письмо к служебной записке?",
        "Какой срок согласования регламента?",
        "Как согласовать большой нормативный документ?",
        "Какой сегодня курс доллара?",
        "Как зарегистрировать входящее письмо?",
    ]
    for q in (sys.argv[1:] or tests):
        if q.startswith("---"):
            print("\n" + q)
            continue
        r = scores(q)[:2]
        a = f"{TYPES[r[0][0]]['id']} {r[0][1]:.3f}"
        b = f"{TYPES[r[1][0]]['id']} {r[1][1]:.3f}"
        print(f"{q[:55]:55} | {a:18} | {b:18} | {'ДА' if classify(q) else 'нет'}")