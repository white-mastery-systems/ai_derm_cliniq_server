"""
models/ai_chat.py — AI Chat Session & Messages
===============================================

Supports the "Ask AI" chat feature on the Case Summary screen.
Unlike the Q&A round messages (Message model), these are free-form
conversational exchanges between the user and Gemini, grounded in
the case's diagnosis and history.

SESSION MODEL
-------------
One session per user per case (created on first message).
The session holds the full conversation history so Gemini can
answer follow-up questions in context:

  Patient: "Tell me the treatment plan"
  AI: "For Cholinergic Urticaria, the main approach is..."
  Patient: "What about side effects of those medications?"
  AI: "The antihistamines mentioned can cause drowsiness..."

Each session belongs to one case + one user. A patient and a doctor
assigned to the same case each get their own separate session.

MESSAGE ROLES
-------------
  user      — message sent by the human (patient or doctor)
  assistant — response from Gemini
"""

import enum

from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class AiChatRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


class AiChatSession(TimestampMixin, Base):
    """
    One chat session per user per case.
    Created automatically on the first message.
    """

    __tablename__ = "ai_chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    case_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    messages: Mapped[list["AiChatMessage"]] = relationship(
        "AiChatMessage",
        back_populates="session",
        order_by="AiChatMessage.created_at",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<AiChatSession id={self.id!r} case={self.case_id!r} user={self.user_id!r}>"


class AiChatMessage(TimestampMixin, Base):
    """
    One message turn in an AI chat session.
    role=user    → message from the patient/doctor
    role=assistant → Gemini's response
    """

    __tablename__ = "ai_chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ai_chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[AiChatRole] = mapped_column(
        Enum(AiChatRole, name="ai_chat_role_enum", create_type=True,
             values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationship
    session: Mapped["AiChatSession"] = relationship("AiChatSession", back_populates="messages")

    def __repr__(self) -> str:
        return f"<AiChatMessage session={self.session_id!r} role={self.role} len={len(self.content)}>"
