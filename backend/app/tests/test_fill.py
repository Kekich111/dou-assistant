import io
import re

import pytest
from docx import Document

from app.api.templates import MODULE_FILES, _find_file
from app.rules import checker, fill

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _sample(mid):
    answers = {}
    for f in fill.MODULES[mid]["fields"]:
        if f["type"] == "choice":
            answers[f["key"]] = f["choices"][0]["value"]
        else:
            value, _ = fill.normalize(f, f.get("example") or "тест")
            answers[f["key"]] = value
    return answers


def _texts(doc):
    out = []
    for p in doc.element.body.iter(W + "p"):
        t = "".join(x.text or "" for x in p.iter(W + "t"))
        if t.strip():
            out.append(t)
    return out


@pytest.mark.parametrize("mid", list(fill.MODULES))
def test_render_leaves_no_blanks(mid):
    path = _find_file(MODULE_FILES[mid])
    if path is None:
        pytest.skip("шаблон не найден")
    data = fill.render(path, mid, _sample(mid))
    texts = _texts(Document(io.BytesIO(data)))
    left = [t for t in texts if re.search(r"_{3,}", t) and not re.fullmatch(r"_+", t.strip())]
    left += [t for t in texts if re.search(r"\[[^\]]*\]", t)]
    assert not left, left[:3]


@pytest.mark.parametrize("mid", list(fill.MODULES))
def test_checker_accepts_filled_document(mid):
    path = _find_file(MODULE_FILES[mid])
    if path is None:
        pytest.skip("шаблон не найден")
    templates = {m: p for m, fn in MODULE_FILES.items() if (p := _find_file(fn))}
    data = fill.render(path, mid, _sample(mid))
    result = checker.check_docx(data, templates, None)
    assert result["module_id"] == mid
    assert result["counts"]["error"] == 0