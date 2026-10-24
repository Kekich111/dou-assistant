"""
backend/app/rag/retriever.py

Поиск по регламенту: вопрос -> dense-вектор (Ollama bge-m3) -> Qdrant top-N
(только is_active=True) -> реранкер -> top-K с релевантностью в процентах.

Запуск из папки backend/:
    python -m app.rag.retriever "Какой срок рассмотрения заявлений?"
    python -m app.rag.retriever "..." --limit 20 --top-k 5 --full
    python -m app.rag.retriever "..." --no-rerank        # только векторный поиск

Переменные .env (все необязательные, по умолчанию — локальный запуск):
    QDRANT_HOST=127.0.0.1
    QDRANT_PORT=6333
    OLLAMA_BASE_URL=http://127.0.0.1:11434
    EMBED_MODEL=bge-m3
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

load_dotenv()

QDRANT_HOST = os.getenv("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")

COLLECTION_NAME = "regulations_v1"
VECTOR_SIZE = 1024

EMBED_TIMEOUT = 60.0
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Hit:
    """Найденный пункт регламента."""

    id: str
    text: str
    chapter: str
    point_no: str
    version_id: str
    vector_score: float                    # косинусная близость из Qdrant
    rerank_score: float | None = None      # релевантность реранкера, 0..100 %
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def passage(self) -> str:
        """Текст для реранкера: тот же контекст (глава + пункт), что и при индексации."""
        return f"{self.chapter}\n{self.text}" if self.chapter else self.text


class Retriever:
    def __init__(
        self,
        host: str = QDRANT_HOST,
        port: int = QDRANT_PORT,
        ollama_url: str = OLLAMA_BASE_URL,
        collection: str = COLLECTION_NAME,
    ) -> None:
        self.collection = collection
        self.ollama_url = ollama_url.rstrip("/")
        self._qdrant = QdrantClient(host=host, port=port, timeout=30)
        self._http = httpx.Client(timeout=EMBED_TIMEOUT)

    # ------------------------------------------------------------------ #
    def embed_query(self, query: str) -> list[float]:
        """Dense-вектор вопроса через Ollama /api/embed (с повторами при сбоях сети)."""
        payload = {"model": EMBED_MODEL, "input": query, "keep_alive": "30m"}
        last_err: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._http.post(f"{self.ollama_url}/api/embed", json=payload)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Ollama {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                vector = resp.json()["embeddings"][0]
                if len(vector) != VECTOR_SIZE:
                    raise ValueError(
                        f"Размерность вектора {len(vector)} != {VECTOR_SIZE}; "
                        f"проверьте модель {EMBED_MODEL!r}"
                    )
                return vector
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_err = exc
                if attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF**attempt
                    log.warning("Ollama embed: попытка %d/%d не удалась (%s), повтор через %.0f c",
                                attempt, MAX_RETRIES, exc, delay)
                    time.sleep(delay)

        raise RuntimeError(f"Ollama недоступна ({self.ollama_url}): {last_err}")

    # ------------------------------------------------------------------ #
    def retrieve(self, query: str, limit: int = 20) -> list[Hit]:
        """Векторный поиск top-limit среди АКТИВНЫХ точек (жёсткий фильтр is_active=True)."""
        query = (query or "").strip()
        if not query:
            raise ValueError("Пустой вопрос")

        vector = self.embed_query(query)
        response = self._qdrant.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=qm.Filter(
                must=[qm.FieldCondition(key="is_active", match=qm.MatchValue(value=True))]
            ),
            limit=limit,
            with_payload=True,
        )

        hits: list[Hit] = []
        for point in response.points:
            payload = dict(point.payload or {})
            hits.append(
                Hit(
                    id=str(point.id),
                    text=payload.get("text", ""),
                    chapter=payload.get("chapter", ""),
                    point_no=payload.get("point_no", ""),
                    version_id=payload.get("version_id", ""),
                    vector_score=float(point.score),
                    payload=payload,
                )
            )
        return hits

    def search(
        self,
        query: str,
        limit: int = 20,
        top_k: int = 5,
        rerank: bool = True,
    ) -> list[Hit]:
        """
        Полный конвейер: top-limit из Qdrant -> реранкер -> top_k.
        Каждый возвращённый Hit содержит rerank_score (0..100 %), если rerank=True.
        Список отсортирован по убыванию релевантности; hits[0].rerank_score
        — значение для порога отказа в guard.py.
        """
        hits = self.retrieve(query, limit=limit)
        if not hits:
            return []
        if not rerank:
            return hits[:top_k]

        from app.rag.reranker import rerank as rerank_passages  # ленивый импорт torch

        results = rerank_passages(query, [h.passage for h in hits], top_k=top_k)
        ranked: list[Hit] = []
        for r in results:
            hit = hits[r.index]
            hit.rerank_score = r.score
            ranked.append(hit)
        return ranked

    def close(self) -> None:
        self._http.close()
        self._qdrant.close()


# --------------------------------------------------------------------------- #
# Модульный синглтон для использования из API
# --------------------------------------------------------------------------- #
_instance: Retriever | None = None
_instance_lock = threading.Lock()


def get_retriever() -> Retriever:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = Retriever()
    return _instance


def search(query: str, limit: int = 20, top_k: int = 5, rerank: bool = True) -> list[Hit]:
    return get_retriever().search(query, limit=limit, top_k=top_k, rerank=rerank)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _bar(percent: float, width: int = 20) -> str:
    filled = round(max(0.0, min(100.0, percent)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _print_hits(query: str, hits: list[Hit], full: bool, elapsed: float) -> None:
    line = "─" * 78
    print(f"\nВопрос: {query}")
    print(f"Найдено: {len(hits)} | время: {elapsed:.2f} c\n{line}")

    if not hits:
        print("Ничего не найдено среди активных пунктов.")
        return

    for rank, hit in enumerate(hits, start=1):
        if hit.rerank_score is not None:
            rel = f"релевантность {hit.rerank_score:5.1f}% {_bar(hit.rerank_score)}"
        else:
            rel = "реранкер отключён"
        print(f"#{rank}  п. {hit.point_no or '—'}   {rel}")
        print(f"    Глава:  {hit.chapter or '—'}")
        print(f"    Cosine: {hit.vector_score:.4f}   Версия: {hit.version_id}")

        body = hit.text if full or len(hit.text) <= 600 else hit.text[:600].rstrip() + "…"
        print(textwrap.indent(textwrap.fill(body, width=72), "    "))
        print(line)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Поиск по регламенту ДОУ")
    parser.add_argument("query", help="Вопрос пользователя")
    parser.add_argument("--limit", type=int, default=20, help="Сколько брать из Qdrant")
    parser.add_argument("--top-k", type=int, default=5, help="Сколько оставить после реранка")
    parser.add_argument("--no-rerank", action="store_true", help="Только векторный поиск")
    parser.add_argument("--full", action="store_true", help="Печатать пункт целиком")
    args = parser.parse_args()

    started = time.perf_counter()
    retriever = Retriever()
    try:
        hits = retriever.search(
            args.query, limit=args.limit, top_k=args.top_k, rerank=not args.no_rerank
        )
    except (RuntimeError, httpx.HTTPError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — Qdrant-ошибки соединения
        print(f"Ошибка при обращении к Qdrant/реранкеру: {exc}", file=sys.stderr)
        return 1
    finally:
        retriever.close()

    _print_hits(args.query, hits, args.full, time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # кириллица и блоки в консоли Windows
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(_main())
