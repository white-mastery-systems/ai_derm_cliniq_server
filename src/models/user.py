"""
models/user.py — User Model
============================

The User table is the authentication anchor for ALL roles.
One row = one account, regardless of whether it belongs to a patient,
doctor, or admin.

ROLE SEPARATION STRATEGY
--------------------------
Instead of three separate user tables (patients_table, doctors_table...),
we use a single `users` table with a `role` column plus separate
*profile* tables for role-specific data.

Why?
- Auth logic (JWT, OAuth, password) is the same for all roles.
- A single table makes cross-role queries simple.
- Profile tables hold only role-specific extended data.

GOOGLE OAUTH
-------------
Users who sign in via Google do NOT have a password_hash.
`google_id` stores the subject identifier from Google's token.
`password_hash` is None for OAuth users.

EMAIL VERIFICATION
-------------------
`is_verified` is set to True after the user clicks the email
confirmation link. Unverified users can log in but may have
limited access (configurable via RBAC).
"""

import enum

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class UserRole(str, enum.Enum):
    """
    User roles that control what actions are allowed.

    str enum → the value stored in the DB is the string itself
    ("patient", "doctor", "admin"), not an integer.
    This makes the DB readable without looking up an enum table.
    """
    PATIENT = "patient"
    DOCTOR = "doctor"
    ADMIN = "admin"


class User(TimestampMixin, Base):
    """
    Core user account. Authentication data only — no clinical data here.

    Relationships:
        patient_profile  — 1-to-1, exists only if role == PATIENT
        doctor_profile   — 1-to-1, exists only if role == DOCTOR
        patient_cases    — all cases where this user is the patient
        doctor_cases     — all cases where this user is the treating doctor
        refresh_tokens   — all refresh tokens issued to this user
    """

    __tablename__ = "users"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_uuid,
        comment="UUID v4 primary key",
    )

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="Unique email address — used for login and notifications",
    )
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Display name shown in the app",
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role_enum", create_type=True),
        nullable=False,
        index=True,
        comment="patient | doctor | admin",
    )

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    password_hash: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="bcrypt hash. NULL for Google OAuth users.",
    )
    google_id: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
        comment="Google subject ID for OAuth users",
    )

    # ------------------------------------------------------------------ #
    # Account State
    # ------------------------------------------------------------------ #
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="False = account suspended. Cannot log in.",
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="True after email confirmation link is clicked",
    )

    # ------------------------------------------------------------------ #
    # Relationships (back-populated in child models)
    # ------------------------------------------------------------------ #
    patient_profile: Mapped["PatientProfile"] = relationship(  # noqa: F821
        "PatientProfile",
        back_populates="user",
        uselist=False,          # 1-to-1
        cascade="all, delete-orphan",
    )
    doctor_profile: Mapped["DoctorProfile"] = relationship(  # noqa: F821
        "DoctorProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    patient_cases: Mapped[list["Case"]] = relationship(  # noqa: F821
        "Case",
        foreign_keys="Case.patient_id",
        back_populates="patient",
    )
    doctor_cases: Mapped[list["Case"]] = relationship(  # noqa: F821
        "Case",
        foreign_keys="Case.doctor_id",
        back_populates="doctor",
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(  # noqa: F821
        "RefreshToken",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    dependents: Mapped[list["Dependent"]] = relationship(  # noqa: F821  # type: ignore[name-defined]
        "Dependent",
        back_populates="patient",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id!r} email={self.email!r} role={self.role}>"
