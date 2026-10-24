"""Разбор инструкции ДОУ на чанки: один пункт регламента = один чанк."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn

DOC_VERSION_ID = "v1.0"
WATERMARK = (
    "УЧЕБНЫЙ ПРИМЕР ДЛЯ ИТ-СОРЕВНОВАНИЙ. НЕ ЯВЛЯЕТСЯ ДЕЙСТВУЮЩИМ ДОКУМЕНТОМ"
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DOCX_PATH = (
    PROJECT_ROOT
    / "data"
    / "regulations"
    / "Инструкция_по_ДОУ_учебный_пример.docx"
)

CHAPTER_RE = re.compile(r"^Глава\s+(\d+)\.\s*(.+)$", re.IGNORECASE)
POINT_RE = re.compile(
    r"^(?:п\.\s*)?(\d+(?:\.\d+)+)\.?\s+(.*)$",
    re.DOTALL,
)
POINT_NO_RE = re.compile(r"^\d+(?:\.\d+)+$")
SENTENCE_END_RE = re.compile(r"[.!?…:;]$")
HYPHEN_END_RE = re.compile(r"[\-­–—]$")
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

Chunk = dict[str, Any]


def chunk_document(
    docx_path: str | Path | None = None,
    *,
    doc_version_id: str = DOC_VERSION_ID,
) -> list[Chunk]:
    """Читает .docx и возвращает список чанков по пунктам регламента."""
    path = Path(docx_path) if docx_path else DEFAULT_DOCX_PATH
    document = Document(path)
    numbering = _NumberingTracker.from_document(document)

    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = _clean_watermark(paragraph.text)
        if not text:
            continue
        point_no = numbering.next_point_no(paragraph)
        if point_no:
            text = f"п. {point_no} {text}"
        lines.append(text)

    merged = _merge_broken_paragraphs(lines)
    chunks = _lines_to_chunks(merged, doc_version_id=doc_version_id)
    return _repair_numbering(chunks)


class _NumberingTracker:
    """Восстанавливает номера пунктов из numbering.xml (python-docx их не отдаёт)."""

    def __init__(self, num_levels: dict[int, dict[int, dict[str, Any]]]) -> None:
        self._num_levels = num_levels
        self._counters: dict[int, dict[int, int]] = {}

    @classmethod
    def from_document(cls, document: Document) -> "_NumberingTracker":
        numbering_part = getattr(document.part, "numbering_part", None)
        if numbering_part is None:
            return cls({})
        return cls(_parse_numbering(numbering_part._element))

    def next_point_no(self, paragraph: Any) -> str | None:
        num_pr = _paragraph_num_pr(paragraph)
        if num_pr is None:
            return None
        num_id, ilvl = num_pr
        levels = self._num_levels.get(num_id)
        if not levels or ilvl not in levels:
            return None
        spec = levels[ilvl]
        if spec["numFmt"] == "bullet":
            return None

        state = self._counters.setdefault(num_id, {})
        state[ilvl] = state.get(ilvl, spec["start"] - 1) + 1
        for deeper in [level for level in state if level > ilvl]:
            del state[deeper]
        for parent in range(ilvl):
            if parent not in state:
                parent_spec = levels.get(parent, {"start": 1})
                state[parent] = int(parent_spec["start"])

        rendered = spec["lvlText"]
        for index in range(9):
            rendered = rendered.replace(f"%{index + 1}", str(state.get(index, 0)))
        rendered = rendered.rstrip(".")
        if not POINT_NO_RE.match(rendered):
            return None
        return rendered


def _parse_numbering(numbering_element: Any) -> dict[int, dict[int, dict[str, Any]]]:
    abstracts: dict[str, dict[int, dict[str, Any]]] = {}
    for abstract in numbering_element.findall(f"{W_NS}abstractNum"):
        abstract_id = abstract.get(qn("w:abstractNumId"))
        levels: dict[int, dict[str, Any]] = {}
        for lvl in abstract.findall(f"{W_NS}lvl"):
            ilvl = int(lvl.get(qn("w:ilvl")) or 0)
            start_el = lvl.find(f"{W_NS}start")
            fmt_el = lvl.find(f"{W_NS}numFmt")
            text_el = lvl.find(f"{W_NS}lvlText")
            start = int((start_el.get(qn("w:val")) if start_el is not None else None) or 1)
            num_fmt = (fmt_el.get(qn("w:val")) if fmt_el is not None else None) or "decimal"
            lvl_text = (text_el.get(qn("w:val")) if text_el is not None else None) or ""
            levels[ilvl] = {"start": start, "numFmt": num_fmt, "lvlText": lvl_text}
        if abstract_id is not None:
            abstracts[str(abstract_id)] = levels

    mapping: dict[int, dict[int, dict[str, Any]]] = {}
    for num in numbering_element.findall(f"{W_NS}num"):
        num_id_raw = num.get(qn("w:numId"))
        abstract_ref = num.find(f"{W_NS}abstractNumId")
        abstract_id = (
            abstract_ref.get(qn("w:val")) if abstract_ref is not None else None
        )
        if num_id_raw is None or abstract_id is None:
            continue
        mapping[int(num_id_raw)] = abstracts.get(str(abstract_id), {})
    return mapping


def _paragraph_num_pr(paragraph: Any) -> tuple[int, int] | None:
    p_pr = paragraph._p.pPr
    if p_pr is None or p_pr.numPr is None:
        return None
    num_pr = p_pr.numPr
    if num_pr.numId is None or num_pr.numId.val is None:
        return None
    ilvl = int(num_pr.ilvl.val) if num_pr.ilvl is not None and num_pr.ilvl.val is not None else 0
    return int(num_pr.numId.val), ilvl


def _clean_watermark(text: str) -> str:
    cleaned = (text or "").replace("\u00a0", " ")
    cleaned = cleaned.replace(WATERMARK, " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _merge_broken_paragraphs(lines: list[str]) -> list[str]:
    """Склеивает разрывы страниц: хвосты заголовков глав и разорванные абзацы."""
    merged: list[str] = []
    for line in lines:
        if not merged:
            merged.append(line)
            continue
        prev = merged[-1]
        if _should_merge(prev, line):
            merged[-1] = _join_text(prev, line)
        else:
            merged.append(line)
    return merged


def _should_merge(prev: str, current: str) -> bool:
    if CHAPTER_RE.match(current) or POINT_RE.match(current):
        return False
    if CHAPTER_RE.match(prev):
        return True
    if HYPHEN_END_RE.search(prev):
        return True
    return not bool(SENTENCE_END_RE.search(prev))


def _join_text(left: str, right: str) -> str:
    if HYPHEN_END_RE.search(left):
        return HYPHEN_END_RE.sub("", left) + right
    if left.endswith("(") or right.startswith(")"):
        return left + right
    return f"{left} {right}".strip()


def _repair_numbering(chunks: list[Chunk]) -> list[Chunk]:
    """Чинит дубли номеров: в DOCX часть пунктов лежит в отдельных списках Word,
    которые перезапускают счёт (пример: пункт 8.12.8 приходит как 8.12.1).
    Если номер не больше предыдущего «брата» с тем же родителем, берём предыдущий + 1."""
    last: dict[tuple[int, ...], int] = {}
    for chunk in chunks:
        meta = chunk["metadata"]
        parts = [int(x) for x in meta["point_no"].split(".")]
        parent = tuple(parts[:-1])
        for key in [k for k in last if len(k) > len(parent)]:
            del last[key]  # вышли на уровень выше - счётчики вложенных сбрасываем
        if parent in last and parts[-1] <= last[parent]:
            old_no = meta["point_no"]
            parts[-1] = last[parent] + 1
            new_no = ".".join(map(str, parts))
            meta["point_no"] = new_no
            meta["numbering_repaired_from"] = old_no
            chunk["text"] = chunk["text"].replace(f"п. {old_no}.", f"п. {new_no}.", 1)
        last[parent] = parts[-1]
    return chunks


def _lines_to_chunks(lines: list[str], *, doc_version_id: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    chapter = ""
    current: Chunk | None = None

    for line in lines:
        chapter_match = CHAPTER_RE.match(line)
        if chapter_match:
            if current is not None:
                chunks.append(current)
                current = None
            chapter = f"Глава {chapter_match.group(1)}. {chapter_match.group(2).strip()}"
            continue

        point_match = POINT_RE.match(line)
        if point_match:
            if current is not None:
                chunks.append(current)
            point_no = point_match.group(1)
            body = point_match.group(2).strip()
            current = {
                "text": f"п. {point_no}. {body}".strip(),
                "metadata": {
                    "chapter": chapter,
                    "point_no": point_no,
                    "doc_version_id": doc_version_id,
                },
            }
            continue

        if current is None:
            continue
        current["text"] = _join_text(current["text"], line)

    if current is not None:
        chunks.append(current)
    return chunks


if __name__ == "__main__":
    result = chunk_document()
    print(f"chunks: {len(result)}")
    if result:
        first = result[0]
        print(first["metadata"])
        print(first["text"][:300])
