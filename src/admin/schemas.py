"""
admin/schemas.py — Admin Dashboard Request & Response Models
=============================================================

ENDPOINTS
---------
GET    /api/v1/admin/users                          → List all users (paginated, filterable)
GET    /api/v1/admin/users/{user_id}                → Get full user detail with profile
PATCH  /api/v1/admin/users/{user_id}                → Update account state (activate/suspend/verify)
DELETE /api/v1/admin/users/{user_id}                → Permanently delete a user
POST   /api/v1/admin/users/{user_id}/resend-verification → Resend email verification OTP
GET    /api/v1/admin/cases                          → List all cases (paginated, filterable)
GET    /api/v1/admin/cases/{case_id}                → Full case detail
GET    /api/v1/admin/stats                          → Platform-wide usage statistics
GET    /api/v1/admin/doctors                        → List all doctors (paginated)
GET    /api/v1/admin/doctors/pending                → List pending approval doctors
POST   /api/v1/admin/doctors/{user_id}/approve      → Approve doctor (activate + email)
POST   /api/v1/admin/doctors/{user_id}/reject       → Reject doctor (delete + email)
GET    /api/v1/admin/prompts                        → List all AI prompt overrides
PATCH  /api/v1/admin/prompts/{key}                  → Set a prompt override in Redis
DELETE /api/v1/admin/prompts/{key}                  → Reset prompt to hardcoded default

DESIGN
------
Admin endpoints are intentionally read-heavy. Writes are limited to
account state changes (activate/suspend/verify) — admins do NOT edit
clinical data (diagnoses, reports, reviews). Those are owned by the
clinical workflow.

PAGINATION CONVENTION (consistent with GET /cases)
---------------------------------------------------
All list endpoints return: { items, total, page, page_size }
"""

from datetime import datetime

from pydantic import BaseModel


# ================================================================== #
# Users
# ================================================================== #

class AdminUserItem(BaseModel):
    """Lightweight user row for the admin user list."""
    id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    is_verified: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminPatientProfileOut(BaseModel):
    patient_code: str
    date_of_birth: str | None = None     # ISO date string
    gender: str | None = None
    phone: str | None = None

    model_config = {"from_attributes": True}


class AdminDoctorProfileOut(BaseModel):
    specialization: str | None = None
    license_number: str | None = None
    clinic_name: str | None = None

    model_config = {"from_attributes": True}


class AdminUserDetail(BaseModel):
    """Full user detail returned by GET /admin/users/{id}."""
    id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    is_verified: bool
    created_at: datetime
    patient_profile: AdminPatientProfileOut | None = None
    doctor_profile: AdminDoctorProfileOut | None = None

    model_config = {"from_attributes": True}


class PaginatedAdminUsersResponse(BaseModel):
    items: list[AdminUserItem]
    total: int
    page: int
    page_size: int


class AdminUpdateUserRequest(BaseModel):
    """
    PATCH /admin/users/{user_id}

    All fields are optional — send only what needs changing.
    Admins can:
    - Activate or suspend an account (is_active)
    - Mark a user as email-verified (is_verified)
    - Change a user's role (use carefully — clinical data ownership changes)
    """
    is_active: bool | None = None
    is_verified: bool | None = None
    role: str | None = None   # "patient" | "doctor" | "admin"


# ================================================================== #
# Cases
# ================================================================== #

class AdminCaseItem(BaseModel):
    """Lightweight case row for the admin case list."""
    id: str
    patient_id: str
    patient_name: str
    doctor_id: str | None
    doctor_name: str | None
    ai_status: str
    clinical_status: str
    consultation_type: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PaginatedAdminCasesResponse(BaseModel):
    items: list[AdminCaseItem]
    total: int
    page: int
    page_size: int


# ================================================================== #
# AI Settings
# ================================================================== #

class AiSettingsResponse(BaseModel):
    """
    Current effective AI model configuration.

    Values shown are the LIVE values (Redis override if set, otherwise .env default).
    The `source` field tells you whether each value came from a runtime override or .env.
    """
    gemini_model: str
    openai_model: str
    deepseek_model: str
    default_provider: str
    env_defaults: dict  # What .env says (for comparison)


class UpdateAiSettingsRequest(BaseModel):
    """
    PATCH /admin/ai-settings

    All fields are optional — send only what needs changing.
    Set a field to null to reset it to the .env default.

    Examples:
        { "gemini_model": "gemini-1.5-pro" }          ← upgrade Gemini model
        { "default_provider": "openai" }               ← switch primary provider
        { "gemini_model": null }                       ← reset Gemini to .env default
    """
    gemini_model: str | None = None
    openai_model: str | None = None
    deepseek_model: str | None = None
    default_provider: str | None = None


# ================================================================== #
# Doctors
# ================================================================== #

class AdminDoctorItem(BaseModel):
    """Doctor list item for GET /admin/doctors."""
    id: str
    email: str
    full_name: str
    is_active: bool
    is_verified: bool
    created_at: datetime
    specialization: str | None = None
    license_number: str | None = None
    clinic_name: str | None = None

    model_config = {"from_attributes": True}


class PaginatedAdminDoctorsResponse(BaseModel):
    items: list[AdminDoctorItem]
    total: int
    page: int
    page_size: int


class RejectDoctorRequest(BaseModel):
    """POST /admin/doctors/{user_id}/reject — optional rejection reason."""
    reason: str | None = None


# ================================================================== #
# Case Detail
# ================================================================== #

class AdminCaseDetail(BaseModel):
    """Full case detail for GET /admin/cases/{case_id}."""
    id: str
    case_number: int | None
    patient_id: str
    patient_name: str
    doctor_id: str | None
    doctor_name: str | None
    consultation_type: str
    has_visible_lesion: bool
    is_for_self: bool
    dependent_name: str | None
    dependent_relationship: str | None
    body_location: str | None
    presenting_complaint: str | None
    case_summary: str | None
    case_title: str | None
    symptom_tags: str | None
    ai_status: str
    clinical_status: str
    red_flag_status: str
    red_flags: str | None
    red_flag_advice: str | None
    question_round: int
    consent_ai_analysis: bool
    consent_research: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ================================================================== #
# AI Prompt Management
# ================================================================== #

class PromptItem(BaseModel):
    """One prompt entry returned by GET /admin/prompts."""
    key: str
    label: str
    value: str | None
    has_override: bool


class PromptsListResponse(BaseModel):
    prompts: list[PromptItem]


class UpdatePromptRequest(BaseModel):
    """PATCH /admin/prompts/{key} — new prompt body."""
    value: str


# ================================================================== #
# Stats
# ================================================================== #

class AdminStatsResponse(BaseModel):
    """Platform-wide statistics for the admin dashboard."""
    # Users
    total_users: int
    total_patients: int
    total_doctors: int
    total_admins: int
    # Cases
    total_cases: int
    cases_ai_pending: int
    cases_ai_processing: int
    cases_ai_completed: int
    cases_ai_failed: int
    # Reports
    total_reports: int
