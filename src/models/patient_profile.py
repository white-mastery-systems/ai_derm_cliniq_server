"""
models/patient_profile.py — Patient Profile
============================================

Extended patient-specific data, separate from the core User.

WHY SEPARATE FROM USER?
------------------------
The `users` table only holds authentication data (email, password, role).
Patient-specific data (DOB, phone, patient_code) lives here so:
- Doctors don't have DOB columns cluttering their profile.
- The users table stays small and fast for auth lookups.
- GDPR: clinical data is isolated from identity data.

PATIENT CODE (e.g. "ABC-5482-S")
----------------------------------
This is the human-readable identifier shown on the patient's Profile
screen. Doctors type this manually into the "Enter Patient Code" field
on their Home screen to pull up a patient without scanning a QR.

Generated once on patient registration, never changes.
Format: 3 uppercase letters + 4 digits + 1 uppercase letter (dash-separated).
Generation logic lives in src/auth/service.py, not here.
"""

from datetime import date

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class PatientProfile(TimestampMixin, Base):
    """
    Extended profile data for patient-role users.

    Linked 1-to-1 with User via user_id.
    Created automatically when a patient registers.
    """

    __tablename__ = "patient_profiles"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Foreign Key — links to users table
    # ------------------------------------------------------------------ #
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,          # enforces 1-to-1 with User
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------ #
    # Patient Code — shown on Profile screen, used by doctors
    # ------------------------------------------------------------------ #
    patient_code: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        index=True,
        comment="Human-readable ID shown on patient profile (e.g. ABC-5482-S). "
                "Doctors use this for manual case lookup.",
    )

    # ------------------------------------------------------------------ #
    # Personal Information — shown on Profile screen
    # ------------------------------------------------------------------ #
    date_of_birth: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Used to compute age displayed in the app",
    )
    gender: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Male | Female | Other | Prefer not to say",
    )
    phone: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        comment="Contact phone number shown on profile",
    )
    avatar_url: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
        comment="GCS signed URL for profile photo",
    )

    # ------------------------------------------------------------------ #
    # Relationship
    # ------------------------------------------------------------------ #
    user: Mapped["User"] = relationship(  # noqa: F821
        "User",
        back_populates="patient_profile",
    )

    def __repr__(self) -> str:
        return f"<PatientProfile user_id={self.user_id!r} code={self.patient_code!r}>"
