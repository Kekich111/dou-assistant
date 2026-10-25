from fastapi import FastAPI
from app.api.chat import router as chat_router  # Импортируем наш роутер чата
from app.api import templates

app = FastAPI(title="Интеллектуальный помощник по ДОУ")

# Подключаем роутер с префиксом /api
app.include_router(chat_router, tags=["Chat"])
app.include_router(templates.router)

@app.get("/health")
def health_check():
    return {"status": "ok"}

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
