"""
cases/schemas.py — Case Request & Response Models
===================================================

CASE LIFECYCLE SUMMARY
-----------------------
1. Patient creates case (POST /cases) → sets consultation_type, is_for_self,
   presenting_complaint, consent_ai_analysis, consent_research.
   Case starts with ai_status=pending, clinical_status=active.
2. Patient uploads images (POST /cases/{id}/images/upload)
4. Patient triggers AI analysis (POST /cases/{id}/ai/analyze) [Layer 5]
5. Doctor scans QR → claims case (PATCH /cases/{id}/assign)
6. Doctor reviews → sets clinical_status badge

SCHEMA DESIGN
--------------
CaseCreateRequest  — minimum fields needed to open a case
CaseUpdateRequest  — PATCH fields (all optional)
CaseResponse       — full case detail returned to client
CaseSummaryResponse — lightweight version for list views (no full text fields)
DoctorStatsResponse — dashboard numbers for the doctor home screen
"""

from datetime import date, datetime

from pydantic import BaseModel, Field


# ================================================================== #
# Create
# ================================================================== #

class DependentInfo(BaseModel):
    """
    Nested model for the 'Someone Else' consultation flow.
    Required when is_for_self=False.
    """
    name: str = Field(min_length=2, max_length=255)
    relationship: str = Field(max_length=100, description="Son | Mother | Spouse etc.")
    date_of_birth: date | None = None
    gender: str | None = Field(default=None, max_length=50)


class CaseCreateRequest(BaseModel):
    """
    POST /api/v1/cases

    Opens a new consultation. Consent is captured here at creation —
    there is no separate consent endpoint.
    consent_ai_analysis must be True or the request is rejected (400 CONSENT_REQUIRED).
    """
    consultation_type: str = Field(
        default="new_complaint",
        pattern="^(new_complaint|follow_up)$",
        description="new_complaint | follow_up",
    )
    has_visible_lesion: bool = Field(
        default=True,
        description="False for complaint-only (no skin image available)",
    )
    is_for_self: bool = Field(
        default=True,
        description="False triggers the 'Someone Else' dependent flow",
    )
    presenting_complaint: str | None = Field(
        default=None,
        max_length=2000,
        description="Patient's initial description in their own words",
    )
    consent_ai_analysis: bool = Field(
        description="Required. Patient consents to AI analysis. Must be True.",
    )
    consent_research: bool = Field(
        default=False,
        description="Optional. Patient allows anonymized data for academic research.",
    )
    dependent: DependentInfo | None = Field(
        default=None,
        description="Required when is_for_self=False",
    )


# ================================================================== #
# Update (PATCH)
# ================================================================== #

class CaseUpdateRequest(BaseModel):
    """
    PATCH /api/v1/cases/{case_id}

    All fields optional — send only what changes.

    clinical_status: doctor-only field — service enforces role check.
    Consent fields are immutable after case creation and cannot be updated here.
    """
    presenting_complaint: str | None = Field(default=None, max_length=2000)
    clinical_status: str | None = Field(
        default=None,
        pattern="^(active|follow_up_available|monitoring|resolved)$",
    )


# ================================================================== #
# Responses
# ================================================================== #

class CaseSummaryResponse(BaseModel):
    """
    Lightweight case item returned in paginated list views.
    Omits large text fields (presenting_complaint, case_summary).
    """
    id: str
    consultation_type: str
    ai_status: str
    clinical_status: str
    is_for_self: bool
    dependent_name: str | None = None
    consent_ai_analysis: bool
    has_visible_lesion: bool
    image_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CaseResponse(BaseModel):
    """
    Full case detail returned by GET /cases/{id} and POST /cases.
    """
    id: str
    patient_id: str
    doctor_id: str | None = None
    consultation_type: str
    has_visible_lesion: bool
    consent_ai_analysis: bool
    consent_ai_analysis_at: datetime | None = None
    consent_research: bool
    is_for_self: bool
    dependent_name: str | None = None
    dependent_relationship: str | None = None
    dependent_dob: date | None = None
    dependent_gender: str | None = None
    ai_status: str
    clinical_status: str
    presenting_complaint: str | None = None
    case_summary: str | None = None
    celery_task_id: str | None = None
    question_round: int
    max_question_rounds: int
    image_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PaginatedCasesResponse(BaseModel):
    """Standard paginated wrapper for case lists."""
    items: list[CaseSummaryResponse]
    total: int
    page: int
    page_size: int
    has_next: bool


class RedFlagsResponse(BaseModel):
    """
    GET /api/v1/cases/{case_id}/red-flags
    POST /api/v1/cases/{case_id}/red-flags/check

    Returned after triggering or reading the systemic / red flag check.
    The Figma Basic Patient Flow shows this as a named step between Q&A and Case Summary.
    """
    case_id: str
    status: str  # not_checked | checking | clear | flagged
    flags: list[str]  # e.g. ["Rapidly changing mole", "Systemic fever"]
    advice: str | None = None  # Shown to patient only when status == flagged
    message: str


class AssessmentDepthRequest(BaseModel):
    """
    POST /api/v1/cases/{case_id}/assessment-depth

    Sets how many Q&A rounds the AI will run.
    Must be called after AI analysis completes and before questions start.
    """
    depth: str = Field(
        pattern="^(quick|standard|full)$",
        description="quick = 2 rounds, standard = 5 rounds, full = 8 rounds",
    )


class AssessmentDepthResponse(BaseModel):
    case_id: str
    depth: str
    max_question_rounds: int
    message: str


class DoctorStatsResponse(BaseModel):
    """
    GET /api/v1/doctors/me/stats

    Numbers shown on the doctor's home screen dashboard.
    """
    today_cases: int
    pending_review: int
    in_progress: int
    completed_today: int
    total_assigned: int


