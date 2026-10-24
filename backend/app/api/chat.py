"""
backend/app/api/chat.py  (v2)

POST /api/chat  {"message": "..."}  ->  ответ + карточки пунктов для CitationCard.

Изменения относительно v1 (контракт расширен, ничего не удалено):
  sources[].quote              — дословная цитата, подтверждающая ответ
  sources[].relevance_percent  — процент для полоски релевантности (0..100)
  elapsed_ms                   — время обработки запроса
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.rag.generator import GenerationError, generate_answer
from app.rag.retriever import search

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

SEARCH_LIMIT = 5

# Временная калибровка процента из косинуса (линейно, с обрезкой по краям).
# Подберите по результатам eval: REL_FLOOR ~ типичный cosine «ловушек»,
# REL_CEIL ~ типичный cosine точных попаданий.
REL_FLOOR = float(os.getenv("REL_FLOOR", "0.35"))
REL_CEIL = float(os.getenv("REL_CEIL", "0.75"))


def relevance_percent(cosine: float, rerank_score: float | None = None) -> float:
    """Если включён реранкер — его score (уже 0..100), иначе линейная калибровка косинуса."""
    if rerank_score is not None:
        return round(rerank_score, 1)
    span = max(REL_CEIL - REL_FLOOR, 1e-6)
    return round(max(0.0, min(1.0, (cosine - REL_FLOOR) / span)) * 100, 1)


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=2000, description="Вопрос пользователя")

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Вопрос не может быть пустым")
        return v


class Source(BaseModel):
    """Данные для карточки CitationCard."""

    point_no: str
    chapter: str
    text: str
    quote: str | None = Field(default=None, description="Дословная цитата из пункта, подтверждающая ответ")
    score: float = Field(description="Косинусная близость из Qdrant, 0..1")
    relevance_percent: float = Field(description="Для полоски релевантности, 0..100")
    rerank_score: float | None = Field(default=None, description="Реранкер, 0..100 % (если включён)")
    version_id: str


class ChatResponse(BaseModel):
    answer: str
    found: bool = Field(description="False — в регламенте нет ответа (отказ)")
    citations: list[str] = Field(description="Номера использованных пунктов")
    sources: list[Source] = Field(description="Карточки процитированных пунктов")
    top_score: float = Field(description="Лучший векторный score среди найденного")
    elapsed_ms: int = Field(description="Время обработки запроса, мс")


# Эндпоинт объявлен через def: поиск и вызов LLM синхронные, FastAPI выполнит
# их в пуле потоков и не заблокирует event loop.
@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    started = time.perf_counter()
    log.info("chat: вопрос длиной %d символов", len(request.message))  # текст не логируем (ПДн)

    try:
        hits = search(request.message, limit=SEARCH_LIMIT, rerank=False)
        result = generate_answer(request.message, hits)
    except GenerationError as exc:
        log.error("Ошибка генерации: %s", exc)
        raise HTTPException(status_code=502, detail="Модель не вернула корректный ответ") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        log.error("Сервис недоступен: %s", exc)
        raise HTTPException(status_code=503, detail="Сервис поиска или модель временно недоступны") from exc
    except Exception as exc:  # noqa: BLE001 — Qdrant и прочее
        log.exception("Непредвиденная ошибка в /api/chat")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера") from exc

    quotes = {e.point_no: e.quote for e in result.evidence}

    return ChatResponse(
        answer=result.answer,
        found=result.found,
        citations=result.citations,
        sources=[
            Source(
                point_no=h.point_no,
                chapter=h.chapter,
                text=h.text,
                quote=quotes.get(h.point_no),
                score=round(h.vector_score, 4),
                relevance_percent=relevance_percent(h.vector_score, h.rerank_score),
                rerank_score=h.rerank_score,
                version_id=h.version_id,
            )
            for h in result.used_hits
        ],
        top_score=round(result.top_score, 4),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
