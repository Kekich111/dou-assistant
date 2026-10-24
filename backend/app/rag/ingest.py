"""
backend/app/rag/ingest.py

Загрузка чанков регламента в Qdrant:
    chunker -> эмбеддинги (Ollama, bge-m3) -> Qdrant (коллекция regulations_v1)

Запуск (из папки backend/):
    python -m app.rag.ingest          # файл по умолчанию: chunker.DEFAULT_DOCX_PATH
    python -m app.rag.ingest --file ... --version-id v1.1 --deactivate-old
    python -m app.rag.ingest --file ... --recreate        # пересоздать коллекцию

Переменные .env:
    QDRANT_HOST=localhost
    QDRANT_PORT=6333
    OLLAMA_BASE_URL=http://localhost:11434
    (опционально) EMBED_MODEL=bge-m3, BATCH_SIZE=16
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.rag.chunker import DEFAULT_DOCX_PATH, DOC_VERSION_ID, chunk_document

load_dotenv()

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")

COLLECTION_NAME = "regulations_v1"
VECTOR_SIZE = 1024  # bge-m3
DEFAULT_VERSION_ID = DOC_VERSION_ID  # "v1.0" из chunker.py
DEFAULT_BATCH_SIZE = int(os.getenv("BATCH_SIZE", "16"))

EMBED_TIMEOUT = 120.0
MAX_RETRIES = 4
RETRY_BACKOFF = 2.0  # секунд, растёт экспоненциально

log = logging.getLogger("ingest")


# --------------------------------------------------------------------------- #
# Получение чанков из chunker.py
# --------------------------------------------------------------------------- #
def load_chunks(docx_path: Path, version_id: str) -> list[dict[str, Any]]:
    """
    chunk_document() возвращает list[{"text": str, "metadata": {chapter, point_no, doc_version_id}}].
    Метаданные разворачиваются в плоский вид, пустые чанки отбрасываются.
    """
    log.info("Парсинг %s через chunker.chunk_document()", docx_path)
    raw = chunk_document(docx_path, doc_version_id=version_id)

    chunks: list[dict[str, Any]] = []
    for item in raw:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        meta = dict(item.get("metadata") or {})
        chunks.append({"text": text, **meta})

    skipped = len(raw) - len(chunks)
    if skipped:
        log.warning("Пропущено пустых чанков: %d", skipped)
    return chunks


def embedding_text(chunk: dict[str, Any]) -> str:
    """Текст для эмбеддинга: глава + пункт. Даёт модели контекст раздела."""
    chapter = chunk.get("chapter") or ""
    return f"{chapter}\n{chunk['text']}" if chapter else chunk["text"]


# --------------------------------------------------------------------------- #
# Эмбеддинги (Ollama, без LangChain)
# --------------------------------------------------------------------------- #
def get_embeddings(texts: list[str], client: httpx.Client) -> list[list[float]]:
    """
    Батч-эмбеддинги через POST /api/embed (Ollama >= 0.3.0).
    Повторяет запрос при таймаутах, сетевых ошибках и 5xx.
    """
    payload = {"model": EMBED_MODEL, "input": texts}
    last_err: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.post(f"{OLLAMA_BASE_URL}/api/embed", json=payload)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"Ollama {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            embeddings = resp.json()["embeddings"]
            if len(embeddings) != len(texts):
                raise ValueError(
                    f"Ollama вернул {len(embeddings)} векторов на {len(texts)} текстов"
                )
            for e in embeddings:
                if len(e) != VECTOR_SIZE:
                    raise ValueError(
                        f"Размерность вектора {len(e)} != {VECTOR_SIZE}. "
                        f"Проверьте модель {EMBED_MODEL!r}."
                    )
            return embeddings
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_err = exc
            if attempt == MAX_RETRIES:
                break
            delay = RETRY_BACKOFF ** attempt
            log.warning(
                "Ollama: попытка %d/%d не удалась (%s). Повтор через %.0f c",
                attempt, MAX_RETRIES, exc, delay,
            )
            time.sleep(delay)

    raise RuntimeError(f"Не удалось получить эмбеддинги после {MAX_RETRIES} попыток: {last_err}")


# --------------------------------------------------------------------------- #
# Qdrant
# --------------------------------------------------------------------------- #
def ensure_collection(qdrant: QdrantClient, recreate: bool = False) -> None:
    exists = qdrant.collection_exists(COLLECTION_NAME)

    if exists and recreate:
        log.warning("Удаляю коллекцию %s (--recreate)", COLLECTION_NAME)
        qdrant.delete_collection(COLLECTION_NAME)
        exists = False

    if not exists:
        log.info("Создаю коллекцию %s (size=%d, COSINE)", COLLECTION_NAME, VECTOR_SIZE)
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=qm.VectorParams(size=VECTOR_SIZE, distance=qm.Distance.COSINE),
        )
    else:
        log.info("Коллекция %s уже существует", COLLECTION_NAME)

    # Payload-индексы (операция идемпотентна)
    qdrant.create_payload_index(
        COLLECTION_NAME, field_name="is_active", field_schema=qm.PayloadSchemaType.BOOL
    )
    qdrant.create_payload_index(
        COLLECTION_NAME, field_name="version_id", field_schema=qm.PayloadSchemaType.KEYWORD
    )
    log.info("Payload-индексы: is_active (bool), version_id (keyword)")


def delete_version(qdrant: QdrantClient, version_id: str) -> None:
    """Удаляет старые точки той же версии, чтобы повторный запуск не плодил дубли."""
    flt = qm.Filter(
        must=[qm.FieldCondition(key="version_id", match=qm.MatchValue(value=version_id))]
    )
    count = qdrant.count(COLLECTION_NAME, count_filter=flt, exact=True).count
    if count:
        log.warning("Версия %s уже загружена (%d точек) — удаляю перед перезаписью", version_id, count)
        qdrant.delete(COLLECTION_NAME, points_selector=qm.FilterSelector(filter=flt), wait=True)


def deactivate_other_versions(qdrant: QdrantClient, active_version: str) -> None:
    """is_active=False для всех точек, у которых version_id != active_version."""
    flt = qm.Filter(
        must_not=[qm.FieldCondition(key="version_id", match=qm.MatchValue(value=active_version))]
    )
    qdrant.set_payload(
        COLLECTION_NAME,
        payload={"is_active": False},
        points=qm.FilterSelector(filter=flt),
        wait=True,
    )
    log.info("Остальные версии помечены is_active=False")


# --------------------------------------------------------------------------- #
# Основной пайплайн
# --------------------------------------------------------------------------- #
def _batched(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _build_payload(chunk: dict[str, Any], version_id: str, source: str, ts: str) -> dict[str, Any]:
    payload = {k: v for k, v in chunk.items() if v is not None}
    payload.update(
        {
            "text": chunk["text"],
            "is_active": True,
            "version_id": version_id,
            "source_file": source,
            "ingested_at": ts,
        }
    )
    return payload


def ingest(
    docx_path: Path,
    version_id: str = DEFAULT_VERSION_ID,
    batch_size: int = DEFAULT_BATCH_SIZE,
    recreate: bool = False,
    deactivate_old: bool = False,
) -> int:
    chunks = load_chunks(docx_path, version_id)
    if not chunks:
        raise RuntimeError("chunker не вернул ни одного чанка")
    log.info("Получено чанков: %d", len(chunks))

    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=60)
    ensure_collection(qdrant, recreate=recreate)
    delete_version(qdrant, version_id)

    ts = datetime.now(timezone.utc).isoformat()
    total = len(chunks)
    n_batches = (total + batch_size - 1) // batch_size
    done = 0
    started = time.perf_counter()

    with httpx.Client(timeout=EMBED_TIMEOUT) as http:
        for b_idx, batch in enumerate(_batched(chunks, batch_size), start=1):
            t0 = time.perf_counter()
            vectors = get_embeddings([embedding_text(c) for c in batch], http)

            points = [
                qm.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vec,
                    payload=_build_payload(c, version_id, docx_path.name, ts),
                )
                for c, vec in zip(batch, vectors)
            ]
            qdrant.upsert(COLLECTION_NAME, points=points, wait=True)

            done += len(batch)
            log.info(
                "Батч %d/%d | загружено %d/%d (%.0f%%) | %.1f c",
                b_idx, n_batches, done, total, done / total * 100, time.perf_counter() - t0,
            )

    if deactivate_old:
        deactivate_other_versions(qdrant, version_id)

    in_db = qdrant.count(COLLECTION_NAME, exact=True).count
    log.info(
        "Готово: %d чанков за %.1f c. Всего точек в %s: %d",
        done, time.perf_counter() - started, COLLECTION_NAME, in_db,
    )
    return done


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Загрузка регламента ДОУ в Qdrant")
    parser.add_argument("--file", type=Path, default=None, help="Путь к .docx инструкции")
    parser.add_argument("--version-id", default=DEFAULT_VERSION_ID)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--recreate", action="store_true", help="Пересоздать коллекцию")
    parser.add_argument(
        "--deactivate-old", action="store_true",
        help="Пометить is_active=False у точек других версий",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    docx_path = args.file or DEFAULT_DOCX_PATH
    if docx_path is None or not docx_path.exists():
        log.error("Файл не найден: %s. Укажите --file путь/к/инструкции.docx", docx_path)
        return 2

    try:
        ingest(
            docx_path,
            version_id=args.version_id,
            batch_size=args.batch_size,
            recreate=args.recreate,
            deactivate_old=args.deactivate_old,
        )
    except Exception:
        log.exception("Ingest завершился с ошибкой")
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(main())
