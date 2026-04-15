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

from src.auth.dependencies import get_current_user, require_doctor, require_patient
from src.cases import service
from src.cases.schemas import (
    AssessmentDepthRequest,
    AssessmentDepthResponse,
    CaseCreateRequest,
    CaseResponse,
    CaseUpdateRequest,
    ComplaintsResponse,
    DoctorCaseCreateRequest,
    DoctorStatsResponse,
    PaginatedCasesResponse,
    PaginatedSearchResponse,
    RedFlagsCheckRequest,
    RedFlagsResponse,
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
)
async def list_cases(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    clinical_status: str | None = Query(default=None),
    is_for_self: bool | None = Query(
        default=None,
        description="true = My History tab, false = Someone Else tab",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> PaginatedCasesResponse:
    return await service.list_cases(db, user, page, page_size, clinical_status, is_for_self)


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
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> ComplaintsResponse:
    return await service.get_complaint_suggestions(db, patient, case_id)


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
    "/{case_id}/assessment-depth",
    response_model=AssessmentDepthResponse,
    status_code=status.HTTP_200_OK,
    summary="Set Q&A depth: quick (2), standard (5), full (8) rounds",
)
async def set_assessment_depth(
    case_id: str,
    request: AssessmentDepthRequest,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> AssessmentDepthResponse:
    return await service.set_assessment_depth(db, patient, case_id, request)


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
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> RedFlagsResponse:
    selected = body.selected_symptoms if body else []
    return await service.trigger_red_flag_check(db, patient, case_id, selected)


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
