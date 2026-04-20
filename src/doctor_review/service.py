"""
doctor_review/service.py — Doctor Review Business Logic
========================================================

Three operations:
1. create_review  — doctor starts reviewing a case
2. update_review  — doctor updates/completes their review
3. get_review     — patient or doctor reads the review

ACCESS RULES
------------
- Only the assigned doctor (case.doctor_id) can create/update the review.
  A case is assigned either via QR scan (auto-assign) or admin PATCH.
- Both the patient who owns the case and the assigned doctor can read the review.
- Other users get 404 (case enumeration prevention).

ONE REVIEW PER CASE
-------------------
DoctorReview has a UNIQUE constraint on case_id.
Attempting to create a second review → 409 Conflict.

COMPLETING THE REVIEW
---------------------
When PATCH sets review_status=COMPLETED:
- review.reviewed_at is recorded
- If clinical_status is provided → case.clinical_status is updated
  (this is what drives the coloured badge in the patient History screen)

SELECTED DIFFERENTIALS & QA HISTORY
-------------------------------------
Both are stored as JSON strings in the DB (Text column).
The service serialises list→JSON on write and deserialises JSON→list on read.
Flutter always sends/receives them as plain lists/dicts.
"""

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.exceptions import (
    CaseNotFoundException,
    ConflictException,
    ForbiddenException,
    NotFoundException,
)
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import Case
from src.models.doctor_review import DoctorReview, ReviewStatus
from src.models.user import User, UserRole
from src.doctor_review.schemas import (
    CreateReviewRequest,
    DoctorReviewResponse,
    UpdateReviewRequest,
)

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _to_response(review: DoctorReview) -> DoctorReviewResponse:
    """Convert ORM row → response schema, deserialising JSON text fields."""
    selected: list[str] = []
    if review.selected_differentials:
        try:
            selected = json.loads(review.selected_differentials)
        except (ValueError, TypeError):
            selected = []

    confirmed: list[str] = []
    if review.confirmed_diagnosis:
        try:
            confirmed = json.loads(review.confirmed_diagnosis)
        except (ValueError, TypeError):
            # backwards-compat: old rows stored a plain string
            confirmed = [review.confirmed_diagnosis]

    qa: list[dict] = []
    if review.qa_history:
        try:
            qa = json.loads(review.qa_history)
        except (ValueError, TypeError):
            qa = []

    indicators: list[str] = []
    if review.clinical_indicators:
        try:
            indicators = json.loads(review.clinical_indicators)
        except (ValueError, TypeError):
            indicators = []

    return DoctorReviewResponse(
        id=review.id,
        case_id=review.case_id,
        doctor_id=review.doctor_id,
        is_ai_correct=review.is_ai_correct,
        selected_differentials=selected,
        confidence_level=review.confidence_level,
        confirmed_diagnosis=confirmed,
        diagnosis_type=review.diagnosis_type,
        review_notes=review.review_notes,
        treatment_plan_json=review.treatment_plan_json,
        qa_history=qa,
        clinical_indicators=indicators,
        review_status=review.review_status,
        reviewed_at=review.reviewed_at,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


async def _load_case_for_doctor(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> Case:
    """
    Load a case and verify the doctor is assigned to it.

    Returns 404 for both 'not found' and 'not assigned' — prevents case
    enumeration (a doctor cannot tell whether a case ID exists or not).
    """
    result = await db.execute(
        select(Case)
        .where(Case.id == case_id)
        .options(selectinload(Case.doctor_review))
    )
    case = result.scalar_one_or_none()

    if case is None or case.doctor_id != doctor.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    return case


# ------------------------------------------------------------------ #
# Create
# ------------------------------------------------------------------ #

async def create_review(
    db: AsyncSession,
    doctor: User,
    case_id: str,
    request: CreateReviewRequest,
) -> DoctorReviewResponse:
    """
    Doctor creates a review for an assigned case.

    Raises 403 if the doctor is not assigned to the case.
    Raises 409 if a review already exists for this case.
    """
    if doctor.role != UserRole.DOCTOR:
        raise ForbiddenException(message="Only doctors can create reviews")

    case = await _load_case_for_doctor(db, doctor, case_id)

    if case.doctor_review is not None:
        raise ConflictException(
            message="A review already exists for this case. Use PATCH to update it."
        )

    review = DoctorReview(
        id=new_uuid(),
        case_id=case_id,
        doctor_id=doctor.id,
        is_ai_correct=request.is_ai_correct,
        selected_differentials=(
            json.dumps(request.selected_differentials)
            if request.selected_differentials is not None else None
        ),
        confidence_level=request.confidence_level,
        confirmed_diagnosis=(
            json.dumps(request.confirmed_diagnosis)
            if request.confirmed_diagnosis is not None
            else json.dumps(request.selected_differentials)
            if request.selected_differentials else None
        ),
        review_notes=request.review_notes,
        treatment_plan_json=request.treatment_plan_json,
        qa_history=(
            json.dumps(request.qa_history)
            if request.qa_history is not None else None
        ),
        clinical_indicators=(
            json.dumps(request.clinical_indicators)
            if request.clinical_indicators is not None else None
        ),
        diagnosis_type=request.diagnosis_type,
        review_status=request.review_status,
    )

    if request.review_status == ReviewStatus.COMPLETED:
        review.reviewed_at = datetime.now(tz=timezone.utc)

    # Sync case_title to first confirmed diagnosis
    title_source = request.confirmed_diagnosis or request.selected_differentials
    if title_source:
        case.case_title = title_source[0]

    db.add(review)
    await db.flush()
    await db.refresh(review)
    logger.info("doctor_review_created", case_id=case_id, doctor_id=doctor.id)
    return _to_response(review)


# ------------------------------------------------------------------ #
# Update
# ------------------------------------------------------------------ #

async def update_review(
    db: AsyncSession,
    doctor: User,
    case_id: str,
    request: UpdateReviewRequest,
) -> DoctorReviewResponse:
    """
    Doctor updates their review (partial update — only provided fields change).

    When review_status → COMPLETED:
    - reviewed_at is set
    - If clinical_status is provided → updates case.clinical_status

    Raises 403 if doctor is not assigned to the case.
    Raises 404 if no review exists yet (create it first with POST).
    """
    if doctor.role != UserRole.DOCTOR:
        raise ForbiddenException(message="Only doctors can update reviews")

    case = await _load_case_for_doctor(db, doctor, case_id)

    if case.doctor_review is None:
        raise NotFoundException(
            message="No review found for this case. Create one first with POST /review."
        )

    review = case.doctor_review

    # Apply partial updates
    if request.is_ai_correct is not None:
        review.is_ai_correct = request.is_ai_correct
    if request.selected_differentials is not None:
        review.selected_differentials = json.dumps(request.selected_differentials)
        # Auto-sync confirmed_diagnosis unless explicitly overridden in this request
        if request.confirmed_diagnosis is None:
            review.confirmed_diagnosis = json.dumps(request.selected_differentials)
            case.case_title = request.selected_differentials[0]
    if request.confidence_level is not None:
        review.confidence_level = request.confidence_level
    if request.confirmed_diagnosis is not None:
        review.confirmed_diagnosis = json.dumps(request.confirmed_diagnosis)
        if request.confirmed_diagnosis:
            case.case_title = request.confirmed_diagnosis[0]
    if request.review_notes is not None:
        review.review_notes = request.review_notes
    if request.treatment_plan_json is not None:
        review.treatment_plan_json = request.treatment_plan_json
    if request.qa_history is not None:
        review.qa_history = json.dumps(request.qa_history)
    if request.clinical_indicators is not None:
        review.clinical_indicators = json.dumps(request.clinical_indicators)
    if request.diagnosis_type is not None:
        review.diagnosis_type = request.diagnosis_type

    if request.review_status is not None:
        review.review_status = request.review_status
        if request.review_status == ReviewStatus.COMPLETED and review.reviewed_at is None:
            review.reviewed_at = datetime.now(tz=timezone.utc)

    # Update case clinical_status when doctor completes review
    if request.clinical_status is not None:
        case.clinical_status = request.clinical_status
        logger.info(
            "clinical_status_updated",
            case_id=case_id,
            clinical_status=request.clinical_status.value,
        )

    logger.info(
        "doctor_review_updated",
        case_id=case_id,
        doctor_id=doctor.id,
        review_status=review.review_status.value,
    )
    return _to_response(review)


# ------------------------------------------------------------------ #
# Read
# ------------------------------------------------------------------ #

async def get_review(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> DoctorReviewResponse:
    """
    Read the doctor review for a case.

    Access:
    - The patient who owns the case
    - The doctor assigned to the case
    - Admin

    Returns 404 if the case doesn't exist, the user doesn't have access,
    or no review has been created yet.
    """
    result = await db.execute(
        select(Case)
        .where(Case.id == case_id)
        .options(selectinload(Case.doctor_review))
    )
    case = result.scalar_one_or_none()

    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    # Access control
    if user.role != UserRole.ADMIN:
        if user.role == UserRole.PATIENT and case.patient_id != user.id:
            raise CaseNotFoundException(message=f"No case found with id: {case_id}")
        if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
            raise ForbiddenException(message="You are not assigned to this case.")

    if case.doctor_review is None:
        raise NotFoundException(
            message="No review has been submitted for this case yet."
        )

    return _to_response(case.doctor_review)
