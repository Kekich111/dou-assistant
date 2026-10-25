import os
import unicodedata
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from fastapi import Response
from pydantic import BaseModel, Field

from app.rules import fill

import logging

from fastapi import File, Form, UploadFile

from app.rules import checker
from app.rules.visas import route_for_module

from urllib.parse import quote

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["templates"])

TEMPLATES_DIR = Path(os.getenv("TEMPLATES_DIR", "/app/data/templates"))

MODULE_FILES = {
    "order_is_commissioning": "Модуль_приказа_о_вводе_в_эксплуатацию_информационной_системы.docx",
    "order_responsible_docflow": "Модуль_приказа_о_назначении_ответственных_за_делопроизводство.docx",
    "order_handover": "Модуль_приказа_о_приеме_и_передаче_дел_и_имущества.docx",
    "order_local_act": "Модуль_приказа_об_утверждении_и_введении_в_действие_локального.docx",
    "decree_process_audit": "Модуль_распоряжения_о_проведении_внутреннего_аудита_процессов.docx",
    "decree_project_committee": "Модуль_распоряжения_о_создании_проектного_комитета.docx",
    "decree_workgroup": "Модуль_распоряжения_о_создании_рабочей_группы.docx",
}


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _find_file(name: str) -> Path | None:
    if not TEMPLATES_DIR.is_dir():
        return None
    target = _nfc(name)
    for f in TEMPLATES_DIR.iterdir():
        if f.is_file() and _nfc(f.name) == target:
            return f
    return None


@router.get("/templates/{module_id}")
def get_template(module_id: str):
    name = MODULE_FILES.get(module_id)
    path = _find_file(name) if name else None
    if not path:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FillStepRequest(BaseModel):
    module_id: str
    answers: dict[str, str] = Field(default_factory=dict)
    key: str | None = None      # на какое поле отвечает пользователь
    value: str | None = None


class FillRenderRequest(BaseModel):
    module_id: str
    answers: dict[str, str]


def _progress(module_id: str, answers: dict) -> dict:
    return {"done": len(answers), "total": len(fill.MODULES[module_id]["fields"])}


@router.post("/fill/step")
def fill_step(req: FillStepRequest):
    if req.module_id not in fill.MODULES:
        raise HTTPException(status_code=404, detail="Для этого модуля диалог пока не готов")
    answers = dict(req.answers)

    if req.key:
        field = fill.get_field(req.module_id, req.key)
        if field is None:
            raise HTTPException(status_code=400, detail="Неизвестное поле")
        value, error = fill.normalize(field, req.value)
        if error:
            return {"status": "invalid", "error": error, "question": fill.public(field),
                    "answers": answers, "progress": _progress(req.module_id, answers)}
        answers[req.key] = value

    field = fill.next_field(req.module_id, answers)
    if field:
        return {"status": "question", "question": fill.public(field),
                "answers": answers, "progress": _progress(req.module_id, answers)}
    return {"status": "done", "answers": answers,
            "summary": fill.summary(req.module_id, answers),
            "warnings": fill.check(req.module_id, answers)}


@router.post("/fill/render")
def fill_render(req: FillRenderRequest):
    if req.module_id not in fill.MODULES:
        raise HTTPException(status_code=404, detail="Модуль не найден")
    if fill.missing_required(req.module_id, req.answers):
        raise HTTPException(status_code=400, detail="Заполнены не все обязательные поля")
    path = _find_file(MODULE_FILES[req.module_id])
    if not path:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    data = fill.render(path, req.module_id, req.answers)
    fname = _download_name(req.module_id)
    disposition = f"attachment; filename=\"document.docx\"; filename*=UTF-8''{quote(fname)}"
    return Response(content=data, media_type=DOCX_MIME,
                    headers={"Content-Disposition": disposition})

MAX_UPLOAD = 5 * 1024 * 1024
MODULE_TITLES = {
    "order_is_commissioning": "Приказ о вводе в эксплуатацию информационной системы",
    "order_responsible_docflow": "Приказ о назначении ответственных за делопроизводство",
    "order_handover": "Приказ о приёме и передаче дел и имущества",
    "order_local_act": "Приказ об утверждении и введении в действие локального акта",
    "decree_process_audit": "Распоряжение о проведении внутреннего аудита процессов",
    "decree_project_committee": "Распоряжение о создании проектного комитета",
    "decree_workgroup": "Распоряжение о создании рабочей группы",
}

def _download_name(module_id: str) -> str:
    title = MODULE_TITLES.get(module_id, "Документ")
    return title.replace(" ", "_") + ".docx"


@router.post("/check")
def check_document(file: UploadFile = File(...), module_id: str | None = Form(None)):
    name = (file.filename or "").lower()
    if not name.endswith(".docx"):
        raise HTTPException(status_code=400, detail="Загрузите файл в формате .docx")
    data = file.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="Файл больше 5 МБ")

    templates = {mid: p for mid, fn in MODULE_FILES.items() if (p := _find_file(fn))}
    try:
        result = checker.check_docx(data, templates, module_id or None)
    except Exception as exc:  # noqa: BLE001
        log.exception("check: не удалось разобрать документ")
        raise HTTPException(status_code=400, detail="Не удалось прочитать документ. Проверьте, что это .docx") from exc

    mid = result["module_id"]
    visa = route_for_module(mid) if mid else None
    result["module_title"] = MODULE_TITLES.get(mid)
    result["steps"] = checker.next_steps(result, visa)
    return result