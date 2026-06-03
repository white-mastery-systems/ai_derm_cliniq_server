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

from typing import Annotated
from pydantic import BaseModel, BeforeValidator, EmailStr, Field


def _blank_to_none(v: object) -> object:
    if isinstance(v, str) and not v.strip():
        return None
    return v


OptionalEmail = Annotated[EmailStr | None, BeforeValidator(_blank_to_none)]

from src.images.schemas import ImageResponse


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
    is_bookmarked: bool = False            # Doctor-set flag — True = shown in Important Cases list
    image_count: int = 0
    patient_name: str | None = None        # Populated for doctor/admin list views
    patient_avatar_url: str | None = None  # Populated for doctor/admin list views
    doctor_name: str | None = None
    doctor_specialization: str | None = None
    doctor_clinic_name: str | None = None
    doctor_avatar_url: str | None = None
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
    case_type: str | None = None
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
    is_bookmarked: bool = False            # Doctor-set flag — True = shown in Important Cases list
    presenting_complaint: str | None = None
    case_summary: str | None = None
    celery_task_id: str | None = None
    question_round: int
    max_question_rounds: int
    image_count: int = 0
    images: list[ImageResponse] = []        # Full image list with signed URLs — always current
    patient_name: str | None = None         # Full name of the patient
    patient_age: int | None = None          # Computed from PatientProfile.date_of_birth
    patient_gender: str | None = None       # From PatientProfile.gender
    patient_avatar_url: str | None = None   # From PatientProfile.avatar_url
    doctor_name: str | None = None          # Full name of the assigned doctor
    doctor_specialization: str | None = None  # From DoctorProfile.specialization
    doctor_clinic_name: str | None = None   # From DoctorProfile.clinic_name
    doctor_avatar_url: str | None = None    # From DoctorProfile.avatar_url
    visit_index: int | None = None          # This case's position in patient's visit history (1-based)
    total_visits: int | None = None         # Total number of cases for this patient
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


class BookmarkResponse(BaseModel):
    """Response for POST /cases/{case_id}/bookmark."""
    case_id: str
    is_bookmarked: bool


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


class SystemicSymptomOption(BaseModel):
    """Single selectable symptom option shown on the Systemic Check screen."""
    id: str
    label: str


class SystemicSymptomsResponse(BaseModel):
    """
    GET /api/v1/cases/{case_id}/systemic-symptoms

    Returns the case-specific symptom options for the Systemic Check screen.
    Generated from the AI differential at the end of save_results_task and
    stored on the case — no extra LLM call at request time.

    Falls back to a generic set if the case analysis has not completed yet.
    Always ends with {"id": "none", "label": "None of the above"}.
    """
    case_id: str
    symptoms: list[SystemicSymptomOption]


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

    Doctor creates a case on behalf of a patient identified by name + email.

    FIND-OR-CREATE LOGIC
    ---------------------
    - Email found in DB → use that patient (must be an active PATIENT account).
      DOB and gender are filled in if missing and provided here.
    - Email not found → a new patient account is created with no password.
      The patient claims their account later via forgot-password OTP.

    Consent is implied by the clinical encounter — consent_ai_analysis = True.
    """
    patient_name: str = Field(
        min_length=2,
        max_length=255,
        description="Patient's full name — used when creating a new account",
    )
    patient_email: OptionalEmail = Field(
        default=None,
        description="Patient's email — used to find or create their account. "
                    "Omit or send an empty string for anonymous/diagnose-only cases.",
    )
    patient_age: int | None = Field(
        default=None,
        ge=0,
        le=120,
        description="Patient's age in years — used when date_of_birth is not available",
    )
    patient_date_of_birth: date | None = Field(
        default=None,
        description="Patient's date of birth — stored in profile if missing. "
                    "Takes precedence over patient_age.",
    )
    patient_gender: str | None = Field(
        default=None,
        max_length=50,
        description="Patient's gender — stored in profile if missing",
    )
    consultation_type: str = Field(
        default="new_complaint",
        pattern="^(new_complaint|follow_up)$",
    )
    has_visible_lesion: bool = Field(default=True)
    body_location: str | None = Field(default=None, max_length=100)
    presenting_complaint: str | None = Field(default=None, max_length=2000)
    consent_research: bool = Field(default=False)
    original_case_id: str | None = Field(
        default=None,
        description="For follow-up consultations — ID of the original case being followed up",
    )
    symptom_progression: str | None = Field(
        default=None,
        pattern="^(better|same|worse)$",
        description="For follow-up consultations — how symptoms changed: better | same | worse",
    )


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


class AdjacentVisitsResponse(BaseModel):
    """
    GET /api/v1/cases/{case_id}/adjacent-visits

    Returns the case_id immediately before and after this one in the patient's
    chronological visit history. Used by the ← → navigation arrows on the
    Case Report screen.

    None means there is no visit in that direction (first or last visit).
    """
    prev_case_id: str | None = None   # None if this is the first visit
    next_case_id: str | None = None   # None if this is the latest visit
    visit_index: int                   # Current visit's 1-based position
    total_visits: int                  # Total visits for this patient


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


# ================================================================== #
# Doctor Diagnose Flow — Visual Findings
# ================================================================== #

class VisualFindingsGenerateResponse(BaseModel):
    """
    POST /api/v1/cases/{case_id}/visual-findings/generate

    Returned immediately (202) when the visual findings Celery task is enqueued.
    Flutter polls GET /cases/{case_id}/ai/status until ai_status == 'completed'.
    """
    case_id: str
    task_id: str
    message: str


class VisualFindingsResponse(BaseModel):
    """
    GET /api/v1/cases/{case_id}/visual-findings

    Returns the three-part visual analysis stored in Case.visual_findings.
    Each section is a dict of structured AI findings (or empty dict if that
    image type was not uploaded).
    """
    case_id: str
    ai_status: str
    clinical: dict = Field(default_factory=dict)
    dermoscopy: dict = Field(default_factory=dict)
    pathology: dict = Field(default_factory=dict)


class VisualFindingsPatchRequest(BaseModel):
    """
    PATCH /api/v1/cases/{case_id}/visual-findings

    Doctor edits the overall_description (clinical) or
    overall_dermoscopic_summary (dermoscopy) text.
    The AI reconcile prompt updates the structured fields to match.
    Only send the section(s) being edited.
    """
    clinical_overall_description: str | None = Field(
        default=None,
        description="Doctor's corrected clinical overall_description — "
                    "triggers AI reconciliation of clinical structured fields",
    )
    dermoscopy_overall_description: str | None = Field(
        default=None,
        description="Doctor's corrected overall_dermoscopic_summary — "
                    "triggers AI reconciliation of dermoscopy structured fields",
    )


class ClinicalFeaturesRequest(BaseModel):
    """
    POST /api/v1/cases/{case_id}/clinical-features

    Doctor submits the confirmed clinical feature checklist after reviewing
    the AI visual findings. Stored in DoctorReview.clinical_indicators.
    """
    features: list[str] = Field(
        min_length=1,
        description="List of clinical feature strings the doctor confirmed (from the checklist)",
    )
    additional_observations: str | None = Field(
        default=None,
        max_length=5000,
        description="Doctor's free-text additional observations",
    )


class ClinicalFeaturesResponse(BaseModel):
    """Response after saving clinical features."""
    case_id: str
    features: list[str]
    additional_observations: str | None = None
    message: str


