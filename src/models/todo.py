"""
models/todo.py — Doctor Clinical Todos
=======================================

Stores clinical action items that the doctor creates for a case.
Todos are the doctor's personal task list — things they need to follow up on.

EXAMPLES
--------
- "Order patch test for contact allergen panel"
- "Schedule follow-up in 2 weeks"
- "Review patient's previous prescription history"
- "Refer to dermatopathology for biopsy"

ONE-TO-MANY WITH CASE
---------------------
One case can have multiple todos.
Todos are created by the assigned doctor and can be seen by the patient.
Only the creating doctor can update or delete their todos.

COMPLETION
----------
is_completed + completed_at track when the todo was finished.
completed_at is set automatically by the service when is_completed → True.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class Todo(TimestampMixin, Base):
    """
    One clinical todo/task for a case.
    """

    __tablename__ = "todos"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Foreign Keys
    # ------------------------------------------------------------------ #
    case_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    doctor_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The doctor who created this todo",
    )

    # ------------------------------------------------------------------ #
    # Content
    # ------------------------------------------------------------------ #
    title: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Short task description (shown as the list item)",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional longer notes / clinical context",
    )
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Optional target date for the action",
    )

    # ------------------------------------------------------------------ #
    # Completion Tracking
    # ------------------------------------------------------------------ #
    is_completed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when is_completed was set to True",
    )

    # ------------------------------------------------------------------ #
    # Relationships
    # ------------------------------------------------------------------ #
    case: Mapped["Case"] = relationship(  # noqa: F821
        "Case",
        back_populates="todos",
    )
    doctor: Mapped["User"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<Todo case={self.case_id!r} "
            f"title={self.title!r} "
            f"completed={self.is_completed}>"
        )
