"""
models/case.py — Case Model
============================

A Case is the central entity of AiDerm Cliniq.
It represents one patient consultation — from image upload through
AI analysis to doctor review and report generation.

TWO STATUS FIELDS (critical — from UI analysis)
------------------------------------------------
The Figma History screen shows status BADGES set by the DOCTOR
("FOLLOW UP AVAILABLE", "MONITORING", "RESOLVED"). These are
completely different from the AI pipeline status.

We use TWO separate enum columns:

1. ai_status  — tracks the Celery task pipeline
   pending → processing → completed | failed

2. clinical_status — set by the doctor after reviewing the case
   active → follow_up_available | monitoring | resolved

Mixing them into one field was an original design mistake that
the Figma screens revealed. Never combine them.

DEPENDENT / "SOMEONE ELSE" FLOW (from UI analysis)
----------------------------------------------------
The Figma Home screen shows "Who is the patient? It is me / Someone else".
When a patient starts a consultation for a family member:
- is_for_self = False
- dependent_* fields capture the family member's details
- The case is still owned by the registered user (patient_id)
- The History screen "Someone Else" tab filters on is_for_self=False

CONSULTATION TYPE
-----------------
"New Complaint" vs "Follow-up" — patient selects at the start.
Affects the AI question flow (follow-up has shorter questioning).

CONSENT GATE
------------
consent_ai_analysis MUST be True before any image can be uploaded.
Both consent fields are submitted at case creation — there is no separate
consent endpoint. consent_ai_analysis is required; consent_research is optional.
This is enforced in the upload endpoint — not just the UI.
"""

import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Sequence, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid

# PostgreSQL sequence for human-readable case numbers shown as "AI-{n}" in the UI
_case_number_seq = Sequence("case_number_seq", start=9001)


class ConsultationType(str, enum.Enum):
    NEW_COMPLAINT = "new_complaint"
    FOLLOW_UP = "follow_up"


class RedFlagStatus(str, enum.Enum):
    """
    Tracks the systemic / red flag check that runs after Q&A completes.
    The check looks for urgent symptoms in the patient's answers and complaint.
    """
    NOT_CHECKED = "not_checked"  # Default — check not yet triggered
    CHECKING = "checking"        # Celery task running
    CLEAR = "clear"              # No red flags found
    FLAGGED = "flagged"          # Urgent symptoms detected


class AiStatus(str, enum.Enum):
    """
    Tracks the Celery AI pipeline state.
    Set by workers — never by the doctor or patient directly.
    """
    PENDING = "pending"         # Case created, waiting to start
    PROCESSING = "processing"   # Celery chain is running
    COMPLETED = "completed"     # All AI tasks finished successfully
    FAILED = "failed"           # A task in the chain failed


class ClinicalStatus(str, enum.Enum):
    """
    Set by the doctor after reviewing the case.
    Shown as coloured badges in the patient History screen.
    """
    ACTIVE = "active"                           # Under review, not yet concluded
    FOLLOW_UP_AVAILABLE = "follow_up_available" # Doctor suggests follow-up (green badge)
    MONITORING = "monitoring"                   # Ongoing observation (orange badge)
    RESOLVED = "resolved"                       # Condition resolved (grey badge)


class Case(TimestampMixin, Base):
    """
    One dermatology consultation, from start to report.

    Relationships:
        patient         — the User who owns this case
        doctor          — the User (doctor) assigned to review
        images          — CaseImage rows (skin photos + prescription)
        visual_descriptions  — AI lesion descriptions (one per analysis round)
        differential_diagnoses — AI differentials (one per round)
        messages        — Patient Q&A conversation history
        doctor_review   — Doctor's answers, confirmed diagnosis
        report          — Generated PDF report
        qr_tokens       — QR codes issued for this case
    """

    __tablename__ = "cases"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Display Number — "AI-9021" shown on case cards
    # ------------------------------------------------------------------ #
    case_number: Mapped[int | None] = mapped_column(
        Integer,
        _case_number_seq,
        server_default=_case_number_seq.next_value(),
        nullable=True,
        unique=True,
        index=True,
        comment="Sequential display number, shown as AI-{n} in the UI",
    )

    # ------------------------------------------------------------------ #
    # Ownership
    # ------------------------------------------------------------------ #
    original_case_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("cases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="For follow-up cases — references the original case being followed up",
    )
    patient_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The registered user who started the consultation",
    )
    doctor_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Assigned doctor. NULL until doctor scans the QR.",
    )

    # ------------------------------------------------------------------ #
    # Consultation Metadata — set at case creation
    # ------------------------------------------------------------------ #
    consultation_type: Mapped[ConsultationType] = mapped_column(
        Enum(ConsultationType, name="consultation_type_enum", create_type=True),
        nullable=False,
        default=ConsultationType.NEW_COMPLAINT,
        comment="new_complaint | follow_up — patient selects at start",
    )
    has_visible_lesion: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Determines which AI path runs (image vs complaint-only)",
    )

    # ------------------------------------------------------------------ #
    # Consent Gate — both fields set at case creation, not a separate call
    # ------------------------------------------------------------------ #
    consent_ai_analysis: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Required. Must be True before any image upload is accepted.",
    )
    consent_ai_analysis_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when patient confirmed AI analysis consent",
    )
    consent_research: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Optional. Patient allows anonymized data for academic research.",
    )

    # ------------------------------------------------------------------ #
    # Dependent / 'Someone Else' flow — from Figma Home screen
    # ------------------------------------------------------------------ #
    is_for_self: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="False when patient consults on behalf of a family member",
    )
    # FK to saved Dependent profile (set when patient picks an existing dependent)
    # Inline columns below are always populated regardless, so queries never need to join.
    dependent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("dependents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    dependent_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Name of the family member (if is_for_self=False)",
    )
    dependent_relationship: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="e.g. Son, Mother, Spouse (if is_for_self=False)",
    )
    dependent_dob: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Date of birth of the family member",
    )
    dependent_gender: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Gender of the family member",
    )

    # ------------------------------------------------------------------ #
    # Body Location — shown as FACE / HAND / BACK etc. tag in case list
    # ------------------------------------------------------------------ #
    body_location: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Body area of the lesion e.g. face, hand, back, arm, leg, neck, chest, other",
    )

    # Follow-up symptom progression — shown as "Follow-up status: Better/Same/Worse"
    # on the Case Summary screen. Only meaningful when consultation_type = follow_up.
    symptom_progression: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Patient-reported symptom change for follow-up cases: better | same | worse",
    )

    # ------------------------------------------------------------------ #
    # Dual Status System — NEVER merge these two fields
    # ------------------------------------------------------------------ #
    ai_status: Mapped[AiStatus] = mapped_column(
        Enum(AiStatus, name="ai_status_enum", create_type=True),
        nullable=False,
        default=AiStatus.PENDING,
        index=True,
        comment="Celery pipeline state. Set by workers only.",
    )
    clinical_status: Mapped[ClinicalStatus] = mapped_column(
        Enum(ClinicalStatus, name="clinical_status_enum", create_type=True),
        nullable=False,
        default=ClinicalStatus.ACTIVE,
        index=True,
        comment="Doctor-set status. Shown as badge in patient History screen.",
    )

    # ------------------------------------------------------------------ #
    # AI-populated display fields — set by save_results_task when AI completes
    # ------------------------------------------------------------------ #
    case_title: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Most probable diagnosis name — shown as card title in case list (e.g. 'Eczema on hands')",
    )
    symptom_tags: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON array of short symptom keywords parsed from key_supporting_features (e.g. ['Redness','Itching','Dry skin'])",
    )

    # ------------------------------------------------------------------ #
    # Clinical Content
    # ------------------------------------------------------------------ #
    presenting_complaint: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Patient's initial complaint in their own words",
    )
    case_summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="AI-generated case summary shown to patient before QR code",
    )

    # ------------------------------------------------------------------ #
    # Red Flag Check — runs after Q&A, before case summary
    # ------------------------------------------------------------------ #
    red_flag_status: Mapped[RedFlagStatus] = mapped_column(
        Enum(RedFlagStatus, name="red_flag_status_enum", create_type=True, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=RedFlagStatus.NOT_CHECKED,
        comment="Status of the systemic / red flag check (Figma Basic Patient Flow step 8)",
    )
    red_flags: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON list of flagged conditions detected by the AI red flag check",
    )
    red_flag_advice: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="AI-generated advice shown to patient when red flags are detected",
    )

    # ------------------------------------------------------------------ #
    # AI Pipeline Tracking
    # ------------------------------------------------------------------ #
    celery_task_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Active Celery task chain ID for status polling",
    )
    question_round: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Current Q&A round number (increments after each question set)",
    )
    max_question_rounds: Mapped[int] = mapped_column(
        Integer,
        default=5,
        nullable=False,
        comment="Set by question_numbers() AI call based on diagnosis complexity",
    )

    # ------------------------------------------------------------------ #
    # Relationships
    # ------------------------------------------------------------------ #
    patient: Mapped["User"] = relationship(  # noqa: F821
        "User",
        foreign_keys=[patient_id],
        back_populates="patient_cases",
    )
    dependent: Mapped["Dependent | None"] = relationship(  # noqa: F821  # type: ignore[name-defined]
        "Dependent",
        foreign_keys=[dependent_id],
        back_populates="cases",
    )
    doctor: Mapped["User | None"] = relationship(  # noqa: F821
        "User",
        foreign_keys=[doctor_id],
        back_populates="doctor_cases",
    )
    images: Mapped[list["CaseImage"]] = relationship(  # noqa: F821
        "CaseImage",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="CaseImage.upload_order",
    )
    visual_descriptions: Mapped[list["VisualDescription"]] = relationship(  # noqa: F821
        "VisualDescription",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="VisualDescription.round_number",
    )
    differential_diagnoses: Mapped[list["DifferentialDiagnosis"]] = relationship(  # noqa: F821
        "DifferentialDiagnosis",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="DifferentialDiagnosis.round_number",
    )
    messages: Mapped[list["Message"]] = relationship(  # noqa: F821
        "Message",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    doctor_review: Mapped["DoctorReview | None"] = relationship(  # noqa: F821
        "DoctorReview",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    report: Mapped["CaseReport | None"] = relationship(  # noqa: F821
        "CaseReport",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    qr_tokens: Mapped[list["QRToken"]] = relationship(  # noqa: F821
        "QRToken",
        back_populates="case",
        cascade="all, delete-orphan",
    )
    todos: Mapped[list["Todo"]] = relationship(  # noqa: F821
        "Todo",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="Todo.created_at",
    )

    def __repr__(self) -> str:
        return (
            f"<Case id={self.id!r} "
            f"ai_status={self.ai_status} "
            f"clinical_status={self.clinical_status}>"
        )
