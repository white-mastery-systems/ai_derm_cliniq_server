"""
doctor_review/controller.py — Doctor Review HTTP Endpoints
===========================================================

All routes are nested under /api/v1/cases/{case_id} (set in src/api.py).

ROUTES
------
POST   /review   → Doctor creates a review for an assigned case (201)
PATCH  /review   → Doctor updates/completes their review (200)
GET    /review   → Patient or doctor reads the review (200)

ROLE RULES
----------
POST/PATCH: DOCTOR only (and must be assigned to the case via QR scan or admin)
GET:        PATIENT (owns the case) or DOCTOR (assigned to the case)
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_doctor
from src.database.core import get_async_session
from src.doctor_review import service
from src.doctor_review.schemas import (
    CreateReviewRequest,
    DoctorReviewResponse,
    UpdateReviewRequest,
)
from src.models.user import User

router = APIRouter()


@router.post(
    "/review",
    response_model=DoctorReviewResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a doctor review for a case",
)
async def create_review(
    case_id: str,
    request: CreateReviewRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> DoctorReviewResponse:
    """
    Doctor creates their review record for an assigned case.

    The doctor must be assigned to the case (via QR scan or admin assignment).
    Returns 403 if not assigned.
    Returns 409 if a review already exists — use PATCH to update.
    """
    return await service.create_review(db, doctor, case_id, request)


@router.patch(
    "/review",
    response_model=DoctorReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a doctor review",
)
async def update_review(
    case_id: str,
    request: UpdateReviewRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> DoctorReviewResponse:
    """
    Partial update to the doctor's review.

    All fields are optional. Only provided fields are changed.
    Setting review_status=completed records the reviewed_at timestamp.
    Setting clinical_status updates the case's clinical badge
    (what the patient sees in their History screen).

    Returns 404 if no review exists yet — create one first with POST.
    """
    return await service.update_review(db, doctor, case_id, request)


@router.get(
    "/review",
    response_model=DoctorReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Get the doctor review for a case",
)
async def get_review(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> DoctorReviewResponse:
    """
    Read the doctor review for a case.

    Accessible to:
    - The patient who owns the case
    - The doctor assigned to the case

    Returns 404 if no review has been submitted yet.
    """
    return await service.get_review(db, user, case_id)
