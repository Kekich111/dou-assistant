from fastapi import FastAPI
from app.api.chat import router as chat_router  # Импортируем наш роутер чата

app = FastAPI(title="Интеллектуальный помощник по ДОУ")

# Подключаем роутер с префиксом /api
app.include_router(chat_router, tags=["Chat"])

@app.get("/health")
def health_check():
    return {"status": "ok"}

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://localhost:3000"],
                   allow_methods=["*"], allow_headers=["*"])
