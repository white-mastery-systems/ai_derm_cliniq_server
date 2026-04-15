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
        description="True = 'Yes, I can see it' (photo upload flow). "
                    "False = 'No visible skin changes' (sensations description flow). "
                    "When False, presenting_complaint is required.",
    )
    is_for_self: bool = Field(
        default=True,
        description="False triggers the 'Someone Else' dependent flow",
    )
    body_location: str | None = Field(
        default=None,
        max_length=100,
        description="Body area of the lesion: face | hand | back | arm | leg | neck | chest | other",
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
    dependent_id: str | None = Field(
        default=None,
        description="ID of a saved Dependent (from GET /users/me/dependents). "
                    "Use this when patient picks an existing dependent from the list. "
                    "Mutually exclusive with inline `dependent` field.",
    )
    dependent: DependentInfo | None = Field(
        default=None,
        description="Inline dependent details — use when creating a new dependent "
                    "without saving first. Mutually exclusive with `dependent_id`.",
    )
    original_case_id: str | None = Field(
        default=None,
        description="For follow-up consultations — ID of the original case being followed up",
    )
    symptom_progression: str | None = Field(
        default=None,
        pattern="^(better|same|worse)$",
        description="For follow-up consultations — how symptoms changed: better | same | worse",
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
    presenting_complaint: list[str] | str | None = Field(default=None)
    body_location: str | None = Field(default=None, max_length=100)
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
    case_number: int | None = None
    display_id: str | None = None          # "AI-9021" — formatted from case_number
    case_title: str | None = None          # Most probable diagnosis — shown as card title
    consultation_type: str
    ai_status: str
    clinical_status: str
    is_for_self: bool
    dependent_name: str | None = None
    dependent_relationship: str | None = None  # e.g. "Son", "Mother" — shown in "(Leo (Son))" label
    consent_ai_analysis: bool
    has_visible_lesion: bool
    body_location: str | None = None
    symptom_progression: str | None = None # For follow-up cases: better | same | worse
    symptom_tags: list[str] = []           # Short symptom keywords for case card chips
    case_summary: str | None = None        # AI-generated summary — used as description text on history cards
    image_count: int = 0
    patient_name: str | None = None        # Populated for doctor/admin list views
    patient_avatar_url: str | None = None  # Populated for doctor/admin list views
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CaseResponse(BaseModel):
    """
    Full case detail returned by GET /cases/{id} and POST /cases.
    """
    id: str
    case_number: int | None = None
    display_id: str | None = None  # "AI-9021"
    case_title: str | None = None  # Most probable diagnosis set after AI completes
    original_case_id: str | None = None
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
    body_location: str | None = None
    symptom_progression: str | None = None  # For follow-up cases: better | same | worse
    symptom_tags: list[str] = []
    presenting_complaint: str | None = None
    case_summary: str | None = None
    celery_task_id: str | None = None
    question_round: int
    max_question_rounds: int
    image_count: int = 0
    patient_name: str | None = None    # Full name of the patient
    patient_age: int | None = None     # Computed from PatientProfile.date_of_birth
    patient_gender: str | None = None  # From PatientProfile.gender
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


class RedFlagsCheckRequest(BaseModel):
    """
    POST /api/v1/cases/{case_id}/red-flags/check

    Optional patient-reported systemic symptoms collected on the Systemic Check
    screen (shown after all Q&A rounds complete).

    The frontend sends the labels the patient selected, e.g.:
      ["Fever or chills", "Night sweats"]

    "None of the above" is filtered out server-side before being passed to the AI.
    An empty list (or omitting the body entirely) means the patient reported nothing.
    """
    selected_symptoms: list[str] = Field(
        default=[],
        description="Symptom labels the patient checked on the Systemic Check screen.",
    )


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

    Patient confirms how many Q&A rounds to run.
    Flutter shows the AI-recommended count (from GET /ai/status)
    and lets the patient adjust with a counter (1–15).
    Must be called after AI analysis completes and before questions start.
    """
    rounds: int = Field(
        ge=1,
        le=15,
        description="Number of Q&A rounds (1–15). Use recommended_rounds from GET /ai/status as default.",
    )


class AssessmentDepthResponse(BaseModel):
    case_id: str
    rounds: int
    max_question_rounds: int
    message: str


class DoctorCaseCreateRequest(BaseModel):
    """
    POST /api/v1/cases/doctor

    Doctor creates a case on behalf of a patient.
    patient_id comes from GET /users/by-code/{patient_code} lookup.
    Consent is implied — the doctor is initiating the clinical workflow.
    """
    patient_id: str = Field(description="UUID of the patient (from patient code lookup)")
    consultation_type: str = Field(
        default="new_complaint",
        pattern="^(new_complaint|follow_up)$",
    )
    has_visible_lesion: bool = Field(default=True)
    body_location: str | None = Field(default=None, max_length=100)
    presenting_complaint: str | None = Field(default=None, max_length=2000)
    consent_research: bool = Field(default=False)


class CaseSearchItem(BaseModel):
    """One case row returned in search results — includes patient name for display."""
    id: str
    case_number: int | None = None
    display_id: str | None = None
    patient_id: str
    patient_name: str
    consultation_type: str
    ai_status: str
    clinical_status: str
    body_location: str | None = None
    presenting_complaint: str | None = None
    image_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class PaginatedSearchResponse(BaseModel):
    items: list[CaseSearchItem]
    total: int
    page: int
    page_size: int
    has_next: bool


class ComplaintsResponse(BaseModel):
    """
    GET /api/v1/cases/{case_id}/complaints

    AI-generated list of presenting complaint options shown as checkboxes
    on the Presenting Complaint screen.

    source = "image"   — generated by analysing uploaded photos (has_visible_lesion=True)
    source = "general" — generated from patient age/sex only (has_visible_lesion=False)

    Flutter combines the checked items + optional free-text and submits them via
    PATCH /cases/{case_id} as presenting_complaint.
    """
    case_id: str
    complaints: list[str]
    source: str  # "image" | "general"


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


