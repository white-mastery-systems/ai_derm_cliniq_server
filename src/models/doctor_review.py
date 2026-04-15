"""
models/doctor_review.py — Doctor Review
=========================================

Stores the doctor's review of a case — their answers to AI questions,
final confirmed diagnosis, and treatment plan.

ONE PER CASE (1-to-1)
----------------------
A case has at most one DoctorReview. The doctor can update it
until review_status = "completed". Once completed, the report
is generated and the case is considered finalised.

REVIEW STATUS
-------------
pending     → Doctor has been assigned but hasn't started reviewing
in_progress → Doctor is actively answering AI clarifying questions
completed   → Doctor confirmed final diagnosis; report can be generated

TREATMENT PLAN AS JSON
----------------------
treatment_plan_json stores the structured treatment plan from
DoctorReviewPrompts.generate_treatment_plan() — medications,
lifestyle modifications, dietary recommendations, and a prescription list.
Stored as TEXT (JSON string) for maximum flexibility.
"""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class DoctorReview(TimestampMixin, Base):
    """
    One doctor review per case.
    """

    __tablename__ = "doctor_reviews"

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
        unique=True,          # 1-to-1 with Case
        nullable=False,
        index=True,
    )
    doctor_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The doctor who performed this review",
    )

    # ------------------------------------------------------------------ #
    # AI Diagnosis Validation — from "AI Diagnosis Correct?" Yes/No UI
    # ------------------------------------------------------------------ #
    is_ai_correct: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        comment="Doctor's verdict: True=AI was correct, False=AI was wrong",
    )
    selected_differentials: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON array of diagnosis names the doctor marked as correct (checkboxes)",
    )
    confidence_level: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Doctor's confidence in confirmed diagnosis: low | high",
    )

    # ------------------------------------------------------------------ #
    # Review Content
    # ------------------------------------------------------------------ #
    confirmed_diagnosis: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Doctor's confirmed final diagnosis (may differ from AI's)",
    )
    review_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Doctor's free-text clinical notes",
    )
    treatment_plan_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON: medications, lifestyle_modifications, dietary_recommendations, prescription[]",
    )

    # ------------------------------------------------------------------ #
    # Doctor Q&A History — rounds of AI-generated clarifying questions
    # ------------------------------------------------------------------ #
    qa_history: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON array of {question, answer} pairs from doctor Q&A rounds",
    )

    # ------------------------------------------------------------------ #
    # Status & Timing
    # ------------------------------------------------------------------ #
    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status_enum", create_type=True, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=ReviewStatus.PENDING,
        index=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when review_status became 'completed'",
    )

    # ------------------------------------------------------------------ #
    # Relationships
    # ------------------------------------------------------------------ #
    case: Mapped["Case"] = relationship(  # noqa: F821
        "Case",
        back_populates="doctor_review",
    )
    doctor: Mapped["User"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<DoctorReview case={self.case_id!r} "
            f"status={self.review_status} "
            f"diagnosis={self.confirmed_diagnosis!r}>"
        )
