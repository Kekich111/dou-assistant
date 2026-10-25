"""
backend/app/api/versions.py

Версионность Инструкции (минимум для демо), источник истины — Qdrant:
    GET  /api/admin/versions                      список версий, какая активна, статус загрузок
    POST /api/admin/versions                      загрузить новую редакцию (.docx) и сделать активной
    POST /api/admin/versions/{version_id}/activate   вернуть (откатить) на выбранную версию

Бот ищет только по точкам с is_active=True (retriever.py), поэтому после
загрузки новой редакции он отвечает по ней, а старая остаётся в истории.

Доступ: заголовок X-Admin-Token должен совпасть с ADMIN_TOKEN из .env.
Если ADMIN_TOKEN не задан, эндпоинты закрыты (503) — на случай, когда
сервис открыт наружу через туннель.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, UploadFile
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.api.chat import clear_answer_cache
from app.rag.ingest import COLLECTION_NAME, QDRANT_HOST, QDRANT_PORT, ingest

log = logging.getLogger(__name__)

UPLOAD_DIR = Path(os.getenv("REGULATIONS_UPLOAD_DIR", "/app/data/regulations/uploads"))
MAX_UPLOAD = 20 * 1024 * 1024
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

# Состояние фоновых загрузок: version_id -> {...}. Живёт до перезапуска backend.
JOBS: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN не задан в .env")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Неверный X-Admin-Token")


router = APIRouter(prefix="/api/admin", tags=["versions"], dependencies=[Depends(require_admin)])


def _qdrant() -> QdrantClient:
    return QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=60)


def _collect_versions(qdrant: QdrantClient) -> dict[str, dict]:
    """Агрегирует точки коллекции по version_id."""
    versions: dict[str, dict] = {}
    if not qdrant.collection_exists(COLLECTION_NAME):
        return versions
    offset = None
    while True:
        points, offset = qdrant.scroll(
            COLLECTION_NAME,
            limit=500,
            offset=offset,
            with_payload=["version_id", "is_active", "source_file", "ingested_at"],
            with_vectors=False,
        )
        for p in points:
            pl = p.payload or {}
            vid = pl.get("version_id") or "?"
            v = versions.setdefault(
                vid,
                {
                    "version_id": vid,
                    "points": 0,
                    "active_points": 0,
                    "source_file": pl.get("source_file"),
                    "ingested_at": pl.get("ingested_at"),
                },
            )
            v["points"] += 1
            if pl.get("is_active"):
                v["active_points"] += 1
        if offset is None:
            break
    for v in versions.values():
        v["is_active"] = v["active_points"] > 0
    return versions


def _run_ingest(path: Path, version_id: str) -> None:
    try:
        n = ingest(path, version_id=version_id, deactivate_old=True)
        clear_answer_cache()  # старые ответы относились к прежней активной версии
        with _jobs_lock:
            JOBS[version_id].update(
                status="done", chunks=n, finished_at=datetime.now(timezone.utc).isoformat()
            )
        log.info("Версия %s загружена (%d чанков), остальные деактивированы", version_id, n)
    except Exception as exc:  # noqa: BLE001
        log.exception("Загрузка версии %s не удалась", version_id)
        with _jobs_lock:
            JOBS[version_id].update(
                status="error", error=str(exc), finished_at=datetime.now(timezone.utc).isoformat()
            )


@router.get("/versions")
def list_versions():
    versions = _collect_versions(_qdrant())
    items = sorted(versions.values(), key=lambda v: v.get("ingested_at") or "", reverse=True)
    with _jobs_lock:
        jobs = {k: dict(v) for k, v in JOBS.items()}
    return {"active": next((v["version_id"] for v in items if v["is_active"]), None),
            "versions": items, "jobs": jobs}


@router.post("/versions", status_code=202)
def upload_version(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    version_id: str = Form(..., description="Например v1.1"),
):
    if not VERSION_RE.match(version_id):
        raise HTTPException(status_code=400, detail="version_id: латиница, цифры, . _ - (до 32 символов)")
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Загрузите файл в формате .docx")

    data = file.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="Файл больше 20 МБ")
    if not data.startswith(b"PK"):  # .docx — это zip
        raise HTTPException(status_code=400, detail="Файл не похож на .docx")

    with _jobs_lock:
        if JOBS.get(version_id, {}).get("status") == "running":
            raise HTTPException(status_code=409, detail=f"Версия {version_id} уже загружается")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{version_id}.docx"
    path.write_bytes(data)
    checksum = hashlib.sha256(data).hexdigest()

    with _jobs_lock:
        JOBS[version_id] = {
            "status": "running",
            "checksum": checksum,
            "filename": file.filename,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    background.add_task(_run_ingest, path, version_id)
    return {
        "status": "running",
        "version_id": version_id,
        "checksum": checksum,
        "detail": "Индексация идёт в фоне; ход смотрите в GET /api/admin/versions. "
                  "Пока она не закончится, активными будут обе версии.",
    }


@router.post("/versions/{version_id}/activate")
def activate_version(version_id: str):
    qdrant = _qdrant()
    target = qm.Filter(must=[qm.FieldCondition(key="version_id", match=qm.MatchValue(value=version_id))])
    if qdrant.count(COLLECTION_NAME, count_filter=target, exact=True).count == 0:
        raise HTTPException(status_code=404, detail=f"Версия {version_id} не найдена в индексе")

    others = qm.Filter(must_not=[qm.FieldCondition(key="version_id", match=qm.MatchValue(value=version_id))])
    qdrant.set_payload(COLLECTION_NAME, payload={"is_active": True},
                       points=qm.FilterSelector(filter=target), wait=True)
    qdrant.set_payload(COLLECTION_NAME, payload={"is_active": False},
                       points=qm.FilterSelector(filter=others), wait=True)
    clear_answer_cache()  # иначе бот отдаст закэшированные ответы прежней версии
    log.info("Активная версия: %s", version_id)
    return {"active": version_id}
