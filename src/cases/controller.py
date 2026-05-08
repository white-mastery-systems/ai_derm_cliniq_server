"""
cases/controller.py — Case HTTP Endpoints
==========================================

All routes prefixed /api/v1/cases (set in src/api.py).

ROUTES
------
POST   /                    → Patient: create a new case (consent fields included)
GET    /                    → Patient/Doctor/Admin: paginated case list
GET    /{case_id}           → Any auth'd user: full case detail
PATCH  /{case_id}           → Patient/Doctor: update fields
DELETE /{case_id}           → Patient/Doctor: soft cancel
PATCH  /{case_id}/assign    → Doctor: claim case after QR scan

Doctor home screen stats:
GET    /doctors/me/stats    → Doctor: dashboard numbers
"""

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_doctor, require_patient, require_patient_or_assigned_doctor
from src.cases import service
from src.cases.schemas import (
    AdjacentVisitsResponse,
    AssessmentDepthRequest,
    AssessmentDepthResponse,
    BookmarkResponse,
    CaseCreateRequest,
    CaseResponse,
    CaseUpdateRequest,
    ClinicalFeaturesRequest,
    ClinicalFeaturesResponse,
    ComplaintsResponse,
    DoctorCaseCreateRequest,
    DoctorStatsResponse,
    PaginatedCasesResponse,
    PaginatedSearchResponse,
    RedFlagsCheckRequest,
    RedFlagsResponse,
    VisualFindingsGenerateResponse,
    VisualFindingsPatchRequest,
    VisualFindingsResponse,
)
from src.database.core import get_async_session
from src.models.user import User

router = APIRouter()


@router.post(
    "",
    response_model=CaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new case",
)
async def create_case(
    request: CaseCreateRequest,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> CaseResponse:
    return await service.create_case(db, patient, request)


@router.get(
    "",
    response_model=PaginatedCasesResponse,
    status_code=status.HTTP_200_OK,
    summary="List cases (role-filtered)",
    description=(
        "Returns paginated cases scoped to the caller's role. "
        "Pass `bookmarked=true` to fetch only the doctor's Important Cases list."
    ),
)
async def list_cases(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    clinical_status: str | None = Query(default=None),
    is_for_self: bool | None = Query(
        default=None,
        description="true = My History tab, false = Someone Else tab",
    ),
    bookmarked: bool | None = Query(
        default=None,
        description="true = Important Cases list (bookmarked by doctor)",
    ),
    assigned_only: bool | None = Query(
        default=None,
        description="true = only cases with a doctor assigned (useful for admin recent cases)",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> PaginatedCasesResponse:
    return await service.list_cases(db, user, page, page_size, clinical_status, is_for_self, bookmarked, assigned_only)


@router.post(
    "/doctor",
    response_model=CaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Doctor creates a case on behalf of a patient",
    description=(
        "Used after the doctor looks up a patient by code (GET /users/by-code/{code}). "
        "Doctor is immediately assigned. Consent is implied by the clinical encounter."
    ),
)
async def create_case_by_doctor(
    request: DoctorCaseCreateRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> CaseResponse:
    return await service.create_case_by_doctor(db, doctor, request)


@router.get(
    "/search",
    response_model=PaginatedSearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Search cases by patient name or case ID",
    description=(
        "Doctor: searches only their assigned cases. "
        "Admin: searches all cases. "
        "Partial, case-insensitive match on patient full name or case ID."
    ),
)
async def search_cases(
    q: str = Query(min_length=1, description="Search term — patient name or case ID"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> PaginatedSearchResponse:
    return await service.search_cases(db, user, q, page, page_size)


@router.get(
    "/doctors/me/stats",
    response_model=DoctorStatsResponse,
    status_code=status.HTTP_200_OK,
    summary="Doctor dashboard statistics",
)
async def get_doctor_stats(
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> DoctorStatsResponse:
    return await service.get_doctor_stats(db, doctor)


@router.get(
    "/{case_id}/complaints",
    response_model=ComplaintsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get AI-generated presenting complaint suggestions",
    description=(
        "Returns a list of complaint options to show as checkboxes on the "
        "Presenting Complaint screen.\n\n"
        "**Visible-lesion flow** (`has_visible_lesion=true`): requires at least one "
        "uploaded image — Gemini analyses the photos and returns image-contextual options.\n\n"
        "**No-lesion flow** (`has_visible_lesion=false`): returns general subjective "
        "complaints based on patient age and sex.\n\n"
        "After the patient selects items and optionally adds free text, submit via "
        "`PATCH /cases/{case_id}` with `presenting_complaint`."
    ),
)
async def get_complaint_suggestions(
    case_id: str,
    user: User = Depends(require_patient_or_assigned_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> ComplaintsResponse:
    return await service.get_complaint_suggestions(db, user, case_id)


@router.get(
    "/{case_id}",
    response_model=CaseResponse,
    status_code=status.HTTP_200_OK,
    summary="Get full case detail",
)
async def get_case(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> CaseResponse:
    return await service.get_case(db, user, case_id)


@router.get(
    "/{case_id}/adjacent-visits",
    response_model=AdjacentVisitsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get prev/next case IDs for visit navigation",
    description=(
        "Returns the case_id immediately before and after this visit in the "
        "patient's chronological history. Used by the ← → arrows on the "
        "Case Report screen. prev_case_id or next_case_id is null when there "
        "is no visit in that direction."
    ),
)
async def get_adjacent_visits(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AdjacentVisitsResponse:
    return await service.get_adjacent_visits(db, user, case_id)


@router.patch(
    "/{case_id}",
    response_model=CaseResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a case (consent, complaint, clinical status)",
)
async def update_case(
    case_id: str,
    request: CaseUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> CaseResponse:
    return await service.update_case(db, user, case_id, request)


@router.delete(
    "/{case_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-cancel a case",
)
async def delete_case(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    await service.soft_delete_case(db, user, case_id)


@router.post(
    "/{case_id}/bookmark",
    response_model=BookmarkResponse,
    status_code=status.HTTP_200_OK,
    summary="Toggle bookmark (Important Case flag)",
    description=(
        "Doctor taps the bookmark icon on a case. "
        "Each call flips the flag: false → true → false. "
        "Only the assigned doctor can bookmark a case. "
        "Use `GET /cases?bookmarked=true` to retrieve all bookmarked cases."
    ),
)
async def toggle_bookmark(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> BookmarkResponse:
    return await service.toggle_bookmark(db, doctor, case_id)


@router.post(
    "/{case_id}/assessment-depth",
    response_model=AssessmentDepthResponse,
    status_code=status.HTTP_200_OK,
    summary="Set Q&A depth: quick (2), standard (5), full (8) rounds",
)
async def set_assessment_depth(
    case_id: str,
    request: AssessmentDepthRequest,
    user: User = Depends(require_patient_or_assigned_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> AssessmentDepthResponse:
    return await service.set_assessment_depth(db, user, case_id, request)


@router.get(
    "/{case_id}/red-flags",
    response_model=RedFlagsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get red flag check status and results",
)
async def get_red_flags(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> RedFlagsResponse:
    return await service.get_red_flags(db, user, case_id)


@router.post(
    "/{case_id}/red-flags/check",
    response_model=RedFlagsResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger systemic / red flag check after Q&A completes",
)
async def trigger_red_flag_check(
    case_id: str,
    body: RedFlagsCheckRequest | None = Body(default=None),
    user: User = Depends(require_patient_or_assigned_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> RedFlagsResponse:
    selected = body.selected_symptoms if body else []
    return await service.trigger_red_flag_check(db, user, case_id, selected)


@router.patch(
    "/{case_id}/assign",
    response_model=CaseResponse,
    status_code=status.HTTP_200_OK,
    summary="Doctor claims case after QR scan",
)
async def assign_doctor(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> CaseResponse:
    return await service.assign_doctor(db, doctor, case_id)


# ------------------------------------------------------------------ #
# Doctor Diagnose Flow — Visual Findings
# ------------------------------------------------------------------ #

@router.post(
    "/{case_id}/visual-findings/generate",
    response_model=VisualFindingsGenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger AI visual findings generation (doctor diagnose flow)",
    description=(
        "Enqueues a Celery task that runs 3-step image analysis: "
        "clinical → dermoscopy → pathology. "
        "At least one clinical, dermoscopy, or pathology image must be uploaded first. "
        "Poll GET /ai/status until ai_status == 'completed', then call "
        "GET /visual-findings to read the results."
    ),
)
async def generate_visual_findings(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> VisualFindingsGenerateResponse:
    return await service.generate_visual_findings(db, doctor, case_id)


@router.get(
    "/{case_id}/visual-findings",
    response_model=VisualFindingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get AI visual findings (doctor diagnose flow)",
    description=(
        "Returns the three-part structured visual analysis stored after "
        "POST /visual-findings/generate completes. "
        "Each section (clinical, dermoscopy, pathology) is a dict of AI findings, "
        "or an empty dict if that image type was not uploaded."
    ),
)
async def get_visual_findings(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> VisualFindingsResponse:
    return await service.get_visual_findings(db, doctor, case_id)


@router.patch(
    "/{case_id}/visual-findings",
    response_model=VisualFindingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Doctor edits visual findings — AI reconciles structured fields",
    description=(
        "When the doctor edits the overall_description (clinical) or "
        "overall_dermoscopic_summary (dermoscopy) text, the AI reconcile prompt "
        "re-derives the structured fields to stay consistent. "
        "Only send the section(s) being edited."
    ),
)
async def patch_visual_findings(
    case_id: str,
    request: VisualFindingsPatchRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> VisualFindingsResponse:
    return await service.patch_visual_findings(db, doctor, case_id, request)


@router.post(
    "/{case_id}/clinical-features",
    response_model=ClinicalFeaturesResponse,
    status_code=status.HTTP_200_OK,
    summary="Save doctor's confirmed clinical feature checklist",
    description=(
        "Doctor submits the confirmed clinical findings from the checklist screen. "
        "Stored in DoctorReview.clinical_indicators. "
        "Creates the DoctorReview row if it does not yet exist."
    ),
)
async def save_clinical_features(
    case_id: str,
    request: ClinicalFeaturesRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> ClinicalFeaturesResponse:
    return await service.save_clinical_features(db, doctor, case_id, request)
