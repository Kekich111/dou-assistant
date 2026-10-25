"""
backend/app/api/chat.py  (v2)

POST /api/chat  {"message": "..."}  ->  ответ + карточки пунктов для CitationCard.

Изменения относительно v1 (контракт расширен, ничего не удалено):
  sources[].quote              — дословная цитата, подтверждающая ответ
  sources[].relevance_percent  — процент для полоски релевантности (0..100)
  elapsed_ms                   — время обработки запроса
"""
from __future__ import annotations
from app.rules.visas import visa_block
from app.rules.clarify import clarify_block

import logging
import os
import time
import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.rag.generator import GenerationError, generate_answer

from app.rag.retriever import search, get_point
from app.rules.related import extra_points

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

SEARCH_LIMIT = int(os.getenv("SEARCH_LIMIT", "3"))

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
    visa: dict | None = Field(default=None, description="Маршрут виз и шаблон, если вопрос про приказ/распоряжение")
    clarify: dict | None = Field(default=None, description="Уточняющий вопрос с вариантами, если вопрос неоднозначен")

def _confidence_percent(conf: float) -> float:
    """Уверенность определения вида документа (косинус 0.5..1.0) -> 0..100 %."""
    return round(max(0.0, min(1.0, (conf - 0.5) / 0.5)) * 100, 1)


def _doc_type_response(question: str, visa: dict, started: float) -> ChatResponse:
    rel = _confidence_percent(visa.get("confidence", 0.0))
    sources: list[Source] = []
    for no in visa.get("points", []):
        try:
            h = get_point(no, question)
        except Exception:  # noqa: BLE001
            log.exception("Пункт %s не получен", no)
            continue
        if h is None:
            continue
        sources.append(
            Source(
                point_no=h.point_no,
                chapter=h.chapter,
                text=h.text,
                quote=None,
                score=round(h.vector_score, 4),
                relevance_percent=rel,
                rerank_score=None,
                version_id=h.version_id,
            )
        )
    titles = ", ".join(t["title"] for t in visa.get("templates", [])) or visa["document_type"]
    answer = f"Похоже, вам нужен документ: {titles}."
    if sources:
        answer += " Что он должен содержать, указано в " + ", ".join("п. " + s.point_no for s in sources) + " Инструкции."
    else:
        answer += " Отдельный пункт Инструкции для этого вида документа не выделен, ориентируйтесь на шаблон модуля."
    answer += " Ниже маршрут визирования по Матрице и шаблон модуля."
    return ChatResponse(
        answer=answer,
        found=True,
        citations=[s.point_no for s in sources],
        sources=sources,
        top_score=round(visa.get("confidence", 0.0), 4),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        visa=visa,
    )


_ANSWER_CACHE: dict[str, "ChatResponse"] = {}
_ANSWER_CACHE_MAX = 200


def _cache_key(q: str) -> str:
    return " ".join(q.lower().split())

# Эндпоинт объявлен через def: поиск и вызов LLM синхронные, FastAPI выполнит
# их в пуле потоков и не заблокирует event loop.
@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    started = time.perf_counter()
    log.info("chat: вопрос длиной %d символов", len(request.message))  # текст не логируем (ПДн)

    key = _cache_key(request.message)
    cached = _ANSWER_CACHE.get(key)
    if cached is not None:
        return cached.model_copy(update={"elapsed_ms": int((time.perf_counter() - started) * 1000)})

    clarify = clarify_block(request.message)
    if clarify:
        visa = None if clarify.get("force") else visa_block(request.message)
        if visa and visa.get("detected_by") == "semantic":
            return _doc_type_response(request.message, visa, started)
        if clarify.get("no_sources"):
            return ChatResponse(
                answer=clarify["intro"], found=True, citations=[], sources=[],
                top_score=0.0, elapsed_ms=int((time.perf_counter() - started) * 1000),
                clarify=clarify,
            )
        try:
            hits = list(search(request.message, limit=SEARCH_LIMIT, rerank=False))
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка поиска в /api/chat (clarify)")
            raise HTTPException(status_code=503, detail="Сервис поиска временно недоступен") from exc
        shown = [h for h in hits if h.point_no in clarify["point_refs"]] or hits[:3]
        return ChatResponse(
            answer=clarify["intro"],
            found=True,
            citations=[h.point_no for h in shown],
            sources=[
                Source(
                    point_no=h.point_no,
                    chapter=h.chapter,
                    text=h.text,
                    quote=None,
                    score=round(h.vector_score, 4),
                    relevance_percent=relevance_percent(h.vector_score, h.rerank_score),
                    rerank_score=h.rerank_score,
                    version_id=h.version_id,
                )
                for h in shown
            ],
            top_score=round(max((h.vector_score for h in hits), default=0.0), 4),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            clarify=clarify,
        )

    visa = visa_block(request.message)
    if visa and visa.get("detected_by") == "semantic":
        return _doc_type_response(request.message, visa, started)

    try:
        hits = list(search(request.message, limit=SEARCH_LIMIT, rerank=False))
        if re.search(r"согласов|визир", request.message, re.I):
            have = {h.point_no for h in hits}
            forced = []
            for no in ("10.3", "10.2"):
                if no not in have:
                    h = get_point(no, request.message)
                    if h is not None:
                        forced.append(h)
            hits = forced + hits
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

    sources = [
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
    ]
    citations = list(result.citations)
    answer = result.answer

    if result.found and citations and len(answer.split()) < 6:
        refs = ", ".join("п. " + c for c in citations)
        if visa:
            answer = f"Что нужно знать об этом документе, указано в {refs} Инструкции. Ниже маршрут визирования по Матрице и шаблон модуля."
        elif quotes.get(citations[0]):
            answer = f"Согласно п. {citations[0]}: «{quotes[citations[0]].strip()}»"
        elif "п." not in answer:
            answer = f"Согласно п. {citations[0]}, {answer.rstrip('.').lower()}."

    if result.found:
        shown = {s.point_no for s in sources}
        added: list[str] = []
        for no in extra_points(request.message):
            if no in shown:
                continue
            try:
                h = get_point(no, request.message)
            except Exception:  # noqa: BLE001 — связанный пункт необязателен
                log.exception("Связанный пункт %s не получен", no)
                continue
            if h is None:
                continue
            sources.append(
                Source(
                    point_no=h.point_no,
                    chapter=h.chapter,
                    text=h.text,
                    quote=None,
                    score=round(h.vector_score, 4),
                    relevance_percent=relevance_percent(h.vector_score, None),
                    rerank_score=None,
                    version_id=h.version_id,
                )
            )
            citations.append(h.point_no)
            added.append(h.point_no)
        if added:
            answer = f"{answer} Связанные пункты Инструкции: {', '.join('п. ' + n for n in added)}."


    found = result.found
    if visa and not found:
        found = True
        answer = f"Нашёл подходящий вид документа: {visa['document_type']}. Ниже маршрут визирования и шаблон."


    response = ChatResponse(
        answer=answer,
        found=found,
        citations=citations,
        sources=sources,
        top_score=round(result.top_score, 4),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        visa=visa,
    )
    if found:
        if len(_ANSWER_CACHE) >= _ANSWER_CACHE_MAX:
            _ANSWER_CACHE.pop(next(iter(_ANSWER_CACHE)))
        _ANSWER_CACHE[key] = response
    return response