import os
from fastapi import FastAPI
from app.api.chat import router as chat_router  # Импортируем наш роутер чата
from app.api import templates, versions
from pathlib import Path
from fastapi.responses import FileResponse

app = FastAPI(title="Интеллектуальный помощник по ДОУ")

# Подключаем роутер с префиксом /api
app.include_router(chat_router, tags=["Chat"])
app.include_router(templates.router)
app.include_router(versions.router)

@app.get("/health")
def health_check():
    return {"status": "ok"}

FRONTEND_DIR = Path(os.getenv("FRONTEND_DIR", "/app/frontend"))

@app.get("/", include_in_schema=False)
def index_page():
    return FileResponse(FRONTEND_DIR / "index.html")

@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(FRONTEND_DIR / "admin.html")

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
