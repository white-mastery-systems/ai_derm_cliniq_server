"""
models/doctor_profile.py — Doctor Profile
==========================================

Extended doctor-specific data, separate from the core User.

Fields visible in the Figma Doctor Profile screen:
- Doctor name (from User.full_name)
- Practice Settings: Account Details, Clinic Schedule, Notifications
- Support links

CLINIC SCHEDULE
---------------
Stored as a JSON string (TEXT column) for flexibility.
The Flutter app can define its own schedule format (e.g. a weekly
availability grid) and send/receive it as a JSON blob.
We store it opaquely — the backend does not parse the schedule.
"""

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class DoctorProfile(TimestampMixin, Base):
    """
    Extended profile data for doctor-role users.

    Linked 1-to-1 with User via user_id.
    Created automatically when a doctor registers.
    """

    __tablename__ = "doctor_profiles"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Foreign Key
    # ------------------------------------------------------------------ #
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------ #
    # Professional Details
    # ------------------------------------------------------------------ #
    specialization: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="e.g. 'Dermatology', 'Trichology'",
    )
    license_number: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Medical license / registration number",
    )
    clinic_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Clinic or hospital name shown in the doctor profile",
    )

    # ------------------------------------------------------------------ #
    # Schedule & Preferences (from Doctor Profile screen)
    # ------------------------------------------------------------------ #
    clinic_schedule: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON blob for weekly schedule. "
                "Parsed and rendered by the Flutter app.",
    )
    notifications_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Push notification preference from Doctor Profile screen",
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
        back_populates="doctor_profile",
    )

    def __repr__(self) -> str:
        return f"<DoctorProfile user_id={self.user_id!r} clinic={self.clinic_name!r}>"
