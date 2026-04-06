"""
cases/controller.py — Case HTTP Endpoints
==========================================

All routes prefixed /api/v1/cases (set in src/api.py).

ROUTES
------
POST   /                    → Patient: create a new case
GET    /                    → Patient/Doctor/Admin: paginated case list
GET    /{case_id}           → Any auth'd user: full case detail
PATCH  /{case_id}           → Patient/Doctor: update fields
DELETE /{case_id}           → Patient/Doctor: soft cancel
PATCH  /{case_id}/assign    → Doctor: claim case after QR scan

Doctor home screen stats:
GET    /doctors/me/stats    → Doctor: dashboard numbers
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_doctor, require_patient
from src.cases import service
from src.cases.schemas import (
    CaseCreateRequest,
    CaseResponse,
    CaseUpdateRequest,
    ConsentResponse,
    DoctorStatsResponse,
    PaginatedCasesResponse,
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> PaginatedCasesResponse:
    return await service.list_cases(db, user, page, page_size, clinical_status)


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
    "/{case_id}/consent",
    response_model=ConsentResponse,
    status_code=status.HTTP_200_OK,
    summary="Patient records informed consent for a case",
)
async def give_consent(
    case_id: str,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> ConsentResponse:
    """
    Record that the patient has read and accepted the consent terms.

    Must be called before images can be uploaded to this case.
    Idempotent — safe to call multiple times; returns already_given=True
    if consent was already recorded.
    """
    return await service.give_consent(db, patient, case_id)


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
