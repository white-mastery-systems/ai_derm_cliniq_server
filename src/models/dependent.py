"""
models/dependent.py — Dependent (Saved Patient) Model
======================================================

A Dependent represents a saved person (child, spouse, parent, etc.)
that a patient can reuse across multiple consultations without re-entering
their details each time.

Shown on the "Select the patient" screen when the user taps "Someone else"
on the home screen. The "+ Add new patient" button creates a new Dependent row.

RELATIONSHIP TO CASE
---------------------
Case.dependent_id is a nullable FK to this table.
When a case is created for an existing dependent, this FK is set.
The inline case columns (dependent_name, dependent_relationship, etc.) are
still populated from the dependent's data — so all existing queries continue
to work without joining this table.

LAST VISIT
----------
Last visit date and diagnosis are NOT stored here — they are computed
at query time by joining to the cases table (most recent case for this dependent).
"""

from datetime import date

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class Dependent(Base, TimestampMixin):
    __tablename__ = "dependents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_uuid
    )
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Male | Female | Other | Prefer not to say",
    )

    # ------------------------------------------------------------------ #
    # Relationships
    # ------------------------------------------------------------------ #
    patient: Mapped["User"] = relationship("User", back_populates="dependents")  # type: ignore[name-defined]
    cases: Mapped[list["Case"]] = relationship("Case", back_populates="dependent")  # type: ignore[name-defined]
