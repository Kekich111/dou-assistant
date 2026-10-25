"""Диалоговое заполнение модульных документов (без LLM).

Бот задаёт вопросы по списку полей (MODULES[...]["fields"]), код проверяет ответы
и подставляет их в DOCX: вместо линий «____» по порядку, вместо вариантов «а / б»
и т. п. Метки «НЕ УДАЛЯТЬ», блок ЭП и «УТВЕРЖДАЮ» не затрагиваются.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date
from pathlib import Path
from string import Formatter

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

log = logging.getLogger(__name__)

BLANK = re.compile(r"_{3,}")
DATE_BLANK = re.compile(r"«_{3}»\s*_{3,}\s*20_{3}\s*г\.")
PERIOD_BLANK = re.compile(r"«_{3}»\s*_{3,}\s*по\s*«_{3}»\s*_{3,}\s*20_{3}\s*г\.")
NUM_PAR = re.compile(r"^(\d+)\.\s")
DATE_IN_TEXT = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

# Описание модулей. Чтобы добавить новый модуль, достаточно добавить сюда запись.
#   fields       — вопросы в порядке показа;
#   slots        — (начало абзаца, N-я линия «___» в нём | "date", шаблон значения);
#   replacements — (начало абзаца или None=везде, что заменить, чем заменить).
# Если для подстановки не хватает ответа (необязательное поле пропущено),
# она пропускается и линия остаётся.
MODULES: dict[str, dict] = {
    "order_is_commissioning": {
        "title": "Приказ о вводе в эксплуатацию",
        "fields": [
            {"key": "kind", "label": "Что вводится", "type": "choice",
             "question": "Что именно вводится в эксплуатацию?",
             "choices": [
                 {"label": "Информационная система", "value": "информационной системы"},
                 {"label": "Подсистема / компонент ИС", "value": "компонента информационной системы"},
                 {"label": "Программный модуль", "value": "программного модуля"},
                 {"label": "Сервис", "value": "сервиса"},
             ]},
            {"key": "full_name", "label": "Полное наименование", "type": "text", "strip_quotes": True,
             "question": "Полное официальное наименование (как в приёмочной документации)?",
             "example": "Электронный архив документов"},
            {"key": "short_name", "label": "Сокращённое наименование", "type": "text", "strip_quotes": True,
             "question": "Сокращённое наименование, которое будет использоваться по всему приказу?",
             "example": "ЭАД"},
            {"key": "ready_doc", "label": "Документ о готовности", "type": "text",
             "question": "Документ о готовности к вводу: вид, дата, номер. Пишите в родительном падеже (на основании чего?).",
             "example": "протокола приёмочных испытаний от 01.10.2026 № 12"},
            {"key": "change_type", "label": "Разработка / изменение", "type": "choice",
             "question": "Компонент создан впервые или это новая версия / доработка?",
             "choices": [
                 {"label": "Создан впервые", "value": "разработанного"},
                 {"label": "Новая версия / доработка", "value": "измененного"},
             ]},
            {"key": "basis_doc", "label": "Документ, определивший работы", "type": "text",
             "question": "В соответствии с каким документом выполнены работы (ТЗ, заявка на изменение)? Дата и номер. Пишите в творительном падеже (в соответствии с чем?).",
             "example": "техническим заданием от 10.03.2026 № 5"},
            {"key": "start_date", "label": "Дата ввода", "type": "date",
             "question": "С какой даты начинается промышленная эксплуатация?",
             "example": "15.11.2026"},
            {"key": "customer", "label": "Функциональный заказчик", "type": "text", "cap_first": True,
             "question": "Какому подразделению поручается применение? Полное название в дательном падеже.",
             "example": "Департаменту документационного обеспечения"},
            {"key": "purpose", "label": "Назначение", "type": "text", "no_end_punct": True,
             "question": "Для чего подразделение будет применять систему? Одной конкретной фразой.",
             "example": "для регистрации и хранения входящих и исходящих документов"},
            {"key": "responsible", "label": "Ответственный за исполнение", "type": "text", "no_end_punct": True,
             "question": "Кто отвечает за исполнение приказа? Должность и ФИО в именительном падеже.",
             "example": "директор Департамента информационных технологий Иванов И. И."},
            {"key": "signer", "label": "Подписант", "type": "text", "optional": True,
             "question": "Инициалы и фамилия подписанта (Заместителя Председателя)? Можно пропустить.",
             "example": "П. П. Петров"},
        ],
        "slots": [
            ("О вводе в эксплуатацию", 0, "{kind} «{full_name}»"),
            ("На основании", 0, "{ready_doc}"),
            ("На основании", 1, "«{full_name}»"),
            ("На основании", 2, "«{short_name}"),      # закрывающая » уже есть в шаблоне
            ("На основании", 3, "{basis_doc}"),
            ("1. Ввести", 0, "{short_name}"),
            ("1. Ввести", "date", "{start_date}"),
            ("2. _", 0, "{customer}"),
            ("обеспечить применение", 0, "{short_name}"),
            ("обеспечить применение", 1, "{purpose}"),
            ("3.1. функционирование", 0, "{short_name}"),
            ("3.2. сопровождение", 0, "{short_name}"),
            ("4. Ответственным", 0, "{responsible}"),
        ],
        "replacements": [
            ("На основании", "разработанного / измененного", "{change_type}"),
            (None, "И. О. Фамилия", "{signer}"),
        ],
    },
}


# ---------------------------------------------------------------- диалог
def get_field(module_id: str, key: str) -> dict | None:
    return next((f for f in MODULES[module_id]["fields"] if f["key"] == key), None)


def next_field(module_id: str, answers: dict) -> dict | None:
    return next((f for f in MODULES[module_id]["fields"] if f["key"] not in answers), None)


def missing_required(module_id: str, answers: dict) -> list[str]:
    return [f["key"] for f in MODULES[module_id]["fields"]
            if not f.get("optional") and not answers.get(f["key"])]


def public(field: dict) -> dict:
    return {k: field.get(k) for k in ("key", "label", "type", "question", "example", "choices", "optional")}


def _parse_date(v: str) -> date | None:
    m = re.fullmatch(r"\s*(\d{1,2})[./](\d{1,2})[./](\d{4})\s*", v)
    if not m:
        return None
    try:
        return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None


def normalize(field: dict, raw: str | None) -> tuple[str | None, str | None]:
    """Возвращает (значение, ошибка)."""
    v = (raw or "").strip()
    if not v:
        return ("", None) if field.get("optional") else (None, "Поле обязательное, напишите ответ.")
    if len(v) > 400:
        return None, "Слишком длинный ответ (до 400 символов)."
    t = field["type"]
    if t == "choice":
        if v not in [c["value"] for c in field["choices"]]:
            return None, "Выберите один из вариантов."
        return v, None
    if t == "date":
        d = _parse_date(v)
        if not d:
            return None, "Не удалось разобрать дату. Напишите в формате ДД.ММ.ГГГГ, например 15.11.2026."
        return d.strftime("%d.%m.%Y"), None
    if field.get("strip_quotes"):
        v = v.strip(' «»"“”')
    if field.get("no_end_punct"):
        v = v.rstrip(" .;,")          # точку в конце ставит сам шаблон
    if field.get("cap_first") and v:
        v = v[0].upper() + v[1:]
    return (v, None) if v else (None, "Поле обязательное, напишите ответ.")


def check(module_id: str, answers: dict) -> list[str]:
    """Предупреждения по содержимому (не блокируют создание документа)."""
    out: list[str] = []
    hook = MODULES[module_id].get("check")
    if hook:
        return hook(answers)
    if module_id != "order_is_commissioning":
        return out

    start = _parse_date(answers["start_date"]) if answers.get("start_date") else None

    ready = answers.get("ready_doc", "")
    found = DATE_IN_TEXT.findall(ready)
    ready_dates = [d for d in (_parse_date(".".join(m)) for m in found) if d]
    if ready and not found:
        out.append("В документе о готовности не видно даты. Для основания приказа нужны вид, дата и номер документа (примечание 2 к модулю).")

    for key, name in (("ready_doc", "документе о готовности"),
                      ("basis_doc", "документе, определившем работы")):
        txt = answers.get(key, "")
        found_k = DATE_IN_TEXT.findall(txt)
        ok_k = [d for d in (_parse_date(".".join(m)) for m in found_k) if d]
        if found_k and len(ok_k) < len(found_k):
            out.append(f"В {name} указана несуществующая дата (например, 31.02). Проверьте её.")

    if start and ready_dates and start < max(ready_dates):
        out.append("Дата ввода раньше даты документа о готовности. По примечанию 8 к модулю так делать нельзя, если иное не предусмотрено порядком ввода в эксплуатацию.")
    if answers.get("short_name") and answers.get("full_name") and len(answers["short_name"]) > len(answers["full_name"]):
        out.append("Сокращённое наименование длиннее полного, проверьте, не перепутаны ли поля.")
    return out


def summary(module_id: str, answers: dict) -> list[dict]:
    rows = []
    for f in MODULES[module_id]["fields"]:
        v = answers.get(f["key"], "")
        if f["type"] == "choice":
            v = next((c["label"] for c in f["choices"] if c["value"] == v), v)
        rows.append({"label": f["label"], "value": v or "—"})
    return rows


# ---------------------------------------------------------------- подстановка в DOCX
def _fmt(template: str, values: dict) -> str | None:
    keys = [k for _, k, _, _ in Formatter().parse(template) if k]
    if any(not values.get(k) for k in keys):
        return None
    return template.format(**values)


def _all_paragraphs(doc) -> list[Paragraph]:
    return [Paragraph(p, doc._body) for p in doc.element.body.iter(qn("w:p"))]


def _drop_notes(doc) -> None:
    """Удаляет раздел «Примечания по заполнению…» и всё после него."""
    start = next((p._p for p in doc.paragraphs if p.text.strip().startswith("Примечания по заполнению")), None)
    if start is None:
        return
    body = doc.element.body
    nodes = [start]
    n = start.getnext()
    while n is not None and n.tag != qn("w:sectPr"):
        nodes.append(n)
        n = n.getnext()
    prev = start.getprevious()
    if (prev is not None and prev.tag == qn("w:p")
            and not "".join(prev.itertext()).strip() and prev.xpath('.//w:br[@w:type="page"]')):
        nodes.append(prev)
    for e in nodes:
        body.remove(e)

def _drop_module_service(doc) -> None:
    """Методрекомендации п. 3.3: убрать гриф утверждения и пометку «МОДУЛЬНЫЙ ДОКУМЕНТ»."""
    for t in list(doc.tables):
        if "УТВЕРЖДАЮ" in "".join(t._tbl.itertext()):
            t._tbl.getparent().remove(t._tbl)
    for p in list(doc.paragraphs):
        if p.text.strip() == "МОДУЛЬНЫЙ ДОКУМЕНТ":
            p._p.getparent().remove(p._p)

def _drop_markers(paragraphs: list[Paragraph]) -> None:
    """Убирает красные надстрочные номера примечаний [1], [2]… и сохраняет пробелы вокруг них."""
    for p in paragraphs:
        pending = False
        last = ""
        for r in list(p.runs):
            t = r.text
            if r.font.superscript and re.fullmatch(r"[\s\[\]\d]*", t):
                pending = pending or " " in t
                r._r.getparent().remove(r._r)
                continue
            if t:
                if pending and last and not last[-1].isspace() and not t[0].isspace() and t[0] not in ",.;:)»":
                    r.text = " " + t
                pending = False
                last = r.text


def _merge_blank_runs(par: Paragraph) -> None:
    """Линия «____», разбитая на два рана, становится одной (иначе она считается за две)."""
    runs = par.runs
    for a, b in zip(runs, runs[1:]):
        if a.text.endswith("_") and b.text.startswith("_"):
            b.text, a.text = "", a.text + b.text


def _fill_blanks(par: Paragraph, mapping: dict[int, str]) -> None:
    _merge_blank_runs(par)
    n = 0
    for run in par.runs:
        old = run.text

        def repl(m):
            nonlocal n
            i, n = n, n + 1
            return mapping.get(i, m.group(0))

        new = BLANK.sub(repl, old)
        if new != old:
            run.text = new


def _find_par(pars: list[Paragraph], anchor: str) -> int | None:
    """Абзац по началу текста. «_#2» — третий по счёту абзац, начинающийся с «_» (ячейки таблиц)."""
    base, sep, occ = anchor.partition("#")
    k = int(occ) if sep else 0
    hits = [i for i, p in enumerate(pars) if p.text.startswith(base)]
    return hits[k] if k < len(hits) else None


def _replace_in_par(par: Paragraph, old: str, new: str) -> bool:
    """Заменяет old на new, даже если old разбит на несколько ранов."""
    runs = par.runs
    pos = "".join(r.text for r in runs).find(old)
    if pos < 0:
        return False
    end, offset, first = pos + len(old), 0, True
    for r in runs:
        t = r.text
        a, b = offset, offset + len(t)
        offset = b
        if b <= pos or a >= end:
            continue
        s, e = max(pos, a) - a, min(end, b) - a
        r.text = (t[:s] + new + t[e:]) if first else (t[:s] + t[e:])
        first = False
    return True


def _apply_optional(pars: list[Paragraph], spec: dict, values: dict) -> bool:
    """Необязательные куски. (абзац, ключ, маркер|None, шаблон):
    ответа нет -> кусок удаляется, есть -> заменяется текстом шаблона.
    marker=None — абзац целиком (потом нужна перенумерация)."""
    removed_par = False
    for anchor, key, marker, tmpl in spec.get("optional", []):
        idx = _find_par(pars, anchor)
        if idx is None:
            log.warning("fill: необязательный абзац «%s» не найден", anchor)
            continue
        p = pars[idx]
        text = _fmt(tmpl, values) if values.get(key) else None
        if marker:
            run = next((r for r in p.runs if marker in r.text and not r.font.superscript), None)
            if run is None:
                continue
            if text is None:
                run._r.getparent().remove(run._r)
            else:
                run.text = text
            continue
        if text is None:
            p._p.getparent().remove(p._p)
            removed_par = True
            continue
        normal = [r for r in p.runs if not r.font.superscript]
        if not normal:
            continue
        m = NUM_PAR.match(normal[0].text)
        normal[0].text = (m.group(0) if m else "") + text
        for r in normal[1:]:
            r._r.getparent().remove(r._r)
    return removed_par


def _drop_rows(pars: list[Paragraph], spec: dict, values: dict) -> None:
    """Строка таблицы удаляется, если поле не заполнено."""
    for anchor, key in spec.get("drop_rows", []):
        if values.get(key):
            continue
        idx = _find_par(pars, anchor)
        if idx is None:
            continue
        tr = next(pars[idx]._p.iterancestors(qn("w:tr")), None)
        if tr is not None and tr.getparent() is not None:
            tr.getparent().remove(tr)


def _renumber(doc) -> None:
    """После удаления пункта: 1., 2., 3. … без пропусков (подпункты 3.1. не трогаем)."""
    n = 0
    for p in doc.paragraphs:
        m = NUM_PAR.match(p.text)
        if not m:
            continue
        n += 1
        if int(m.group(1)) != n:
            for r in p.runs:
                if re.match(r"^\d+\.", r.text):
                    r.text = re.sub(r"^\d+\.", f"{n}.", r.text, count=1)
                    break


def _period_text(a: date, b: date) -> str:
    if a.year == b.year:
        return f"«{a.day:02d}» {MONTHS[a.month - 1]} по «{b.day:02d}» {MONTHS[b.month - 1]} {b.year} г."
    return (f"«{a.day:02d}» {MONTHS[a.month - 1]} {a.year} г. по "
            f"«{b.day:02d}» {MONTHS[b.month - 1]} {b.year} г.")


def render(template_path: Path | str, module_id: str, answers: dict) -> bytes:
    spec = MODULES[module_id]
    values = dict(answers)
    for f in spec["fields"]:                       # дата -> «15» ноября 2026 г.
        if f["type"] == "date" and values.get(f["key"]):
            d = _parse_date(values[f["key"]])
            values[f["key"]] = f"«{d.day:02d}» {MONTHS[d.month - 1]} {d.year} г."
    derive = spec.get("derive")
    if derive:
        derive(values, answers)

    doc = Document(str(template_path))
    _drop_notes(doc)
    _drop_module_service(doc)
    pars = _all_paragraphs(doc)

    removed_par = _apply_optional(pars, spec, values)
    _drop_rows(pars, spec, values)

    # Сначала находим абзацы (пока текст не менялся), потом подставляем.
    blanks: dict[int, dict[int, str]] = {}
    dates: list[tuple[int, str, re.Pattern]] = []
    for anchor, sel, tmpl in spec["slots"]:
        text = _fmt(tmpl, values)
        idx = _find_par(pars, anchor)
        if idx is None:
            log.warning("fill: абзац «%s» не найден в шаблоне %s", anchor, module_id)
            continue
        if text is None:
            continue
        if sel == "date":
            dates.append((idx, text, DATE_BLANK))
        elif sel == "period":
            dates.append((idx, text, PERIOD_BLANK))
        else:
            blanks.setdefault(idx, {})[sel] = text

    for idx, text, rx in dates:
        for run in pars[idx].runs:
            if rx.search(run.text):
                run.text = rx.sub(lambda _m: text, run.text, count=1)
                break
    for idx, mapping in blanks.items():
        _fill_blanks(pars[idx], mapping)

    for anchor, old, tmpl in spec.get("replacements", []):
        text = _fmt(tmpl, values)
        if text is None:
            continue
        for p in pars:
            if anchor and not p.text.startswith(anchor):
                continue
            while old in p.text and _replace_in_par(p, old, text):
                pass

    _drop_markers(pars)
    if removed_par:
        _renumber(doc)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


from app.rules.fill_modules import EXTRA_MODULES  # noqa: E402  (описания остальных модулей)

MODULES.update(EXTRA_MODULES)


