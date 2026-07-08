from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ConversationMessage(Base):
    """Full chat history. The most recent messages are replayed to Claude;
    older spans are periodically summarized into MemoryNote rows."""

    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)
    summarized: Mapped[bool] = mapped_column(default=False)


class MemoryNote(Base):
    """Durable long-term memory the AI writes via the remember_fact tool:
    lifestyle facts, favorite recipes, family context, conversation summaries."""

    __tablename__ = "memory_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # tercih, aliskanlik, saglik, aile, yasam, ozet, diger
    category: Mapped[str] = mapped_column(String(32), default="diger")
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True)
