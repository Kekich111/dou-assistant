"""
backend/app/db/init_db.py
Скрипт прямого создания таблиц в PostgreSQL без Alembic.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine

# Поднимаемся к корню бэкенда, чтобы подтянуть .env
backend_root = Path(__file__).resolve().parents[2]
load_dotenv(backend_root / ".env")

# Подтягиваем модели и базовый класс
sys.path.append(str(backend_root))
from app.db.models import Base

# Настройки подключения к Docker-контейнеру
DB_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "postgres")
DB_DB   = os.getenv("POSTGRES_DB", "fintech_doc_assistant")

DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_DB}"

def init_database():
    print(f"Подключение к БД: {DB_HOST}:{DB_PORT}...")
    try:
        engine = create_engine(DATABASE_URL)
        
        print("Создание таблиц в PostgreSQL...")
        Base.metadata.create_all(bind=engine)
        
        print("🎉 Успех! Все 6 таблиц (users, documents, document_versions, "
              "chat_sessions, chat_messages, audit_logs) успешно созданы!")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}", file=sys.stderr)

init_database()

