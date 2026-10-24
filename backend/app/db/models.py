"""
backend/app/db/models.py

Реляционная схема (PostgreSQL 16, SQLAlchemy 2.0):

    User 1──* Document 1──* DocumentVersion
    User 1──* ChatSession 1──* ChatMessage
    User 1──* AuditLog   (user_id nullable: гости и удалённые пользователи)

Модуль не создаёт engine и не ходит в БД при импорте, поэтому безопасно
импортируется из FastAPI, Alembic (target_metadata = Base.metadata) и тестов.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "Base",
    "UserRole",
    "MessageRole",
    "User",
    "Document",
    "DocumentVersion",
    "ChatSession",
    "ChatMessage",
    "AuditLog",
]

# --------------------------------------------------------------------------- #
# Базовый класс и общие типы
# --------------------------------------------------------------------------- #
# Единые имена ограничений: без них Alembic не сможет надёжно их удалять/менять.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# BIGINT для быстрорастущих таблиц; на SQLite (тесты) откатываемся к INTEGER,
# иначе не работает автоинкремент.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")

# JSONB в PostgreSQL, обычный JSON в остальных СУБД.
JsonType = JSON().with_variant(JSONB(), "postgresql")


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """VARCHAR вместо нативного ENUM PostgreSQL: добавлять роли миграцией проще."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=16,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


# --------------------------------------------------------------------------- #
# Пользователи
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        _enum(UserRole, "user_role"),
        default=UserRole.user,
        server_default=UserRole.user.value,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    documents: Mapped[list["Document"]] = relationship(back_populates="uploader")
    chat_sessions: Mapped[list["ChatSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="user")

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role.value}>"


# --------------------------------------------------------------------------- #
# Документы и версии
# --------------------------------------------------------------------------- #
class Document(Base):
    """Логический документ (например, «Инструкция по ДОУ»); версии лежат в DocumentVersion."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    uploader: Mapped["User | None"] = relationship(back_populates="documents")
    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentVersion.uploaded_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<Document id={self.id} name={self.name!r}>"


class DocumentVersion(Base):
    """
    Версия регламента. version_id совпадает с payload-полем version_id в Qdrant.
    У документа может быть не более одной активной версии (частичный уникальный индекс).
    """

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_id", name="uq_document_versions_doc_version"),
        Index(
            "uq_document_versions_one_active",
            "document_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    version_id: Mapped[str] = mapped_column(String(32))  # "v1.0"
    checksum: Mapped[str] = mapped_column(String(64))  # SHA-256 файла, hex
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    document: Mapped["Document"] = relationship(back_populates="versions")
    uploader: Mapped["User | None"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<DocumentVersion id={self.id} doc={self.document_id} "
            f"version={self.version_id!r} active={self.is_active}>"
        )


# --------------------------------------------------------------------------- #
# Диалоги
# --------------------------------------------------------------------------- #
class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(
        String(255), default="Новый диалог", server_default="Новый диалог"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="chat_sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatMessage.id",
    )

    def __repr__(self) -> str:
        return f"<ChatSession id={self.id} user={self.user_id} title={self.title!r}>"


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MessageRole] = mapped_column(_enum(MessageRole, "message_role"))
    content: Mapped[str] = mapped_column(Text)
    # Номера пунктов, на которые опирался ответ, например ["10.3", "6.6"].
    citations: Mapped[list[str]] = mapped_column(
        JsonType, default=list, server_default=text("'[]'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    session: Mapped["ChatSession"] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<ChatMessage id={self.id} session={self.session_id} role={self.role.value}>"


# --------------------------------------------------------------------------- #
# Аудит
# --------------------------------------------------------------------------- #
class AuditLog(Base):
    """
    Журнал действий для middleware. Записи переживают удаление пользователя
    (ondelete SET NULL). Приложение должно только добавлять строки, не менять и не удалять.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )  # NULL — гость / неаутентифицированный запрос
    action: Mapped[str] = mapped_column(String(64), index=True)  # "POST /api/chat", "login", ...
    resource: Mapped[str] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(45))  # IPv4/IPv6
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    request_hash: Mapped[str | None] = mapped_column(String(64))  # SHA-256 запроса, hex

    user: Mapped["User | None"] = relationship(back_populates="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} user={self.user_id} action={self.action!r}>"
