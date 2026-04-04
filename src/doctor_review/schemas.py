"""
doctor_review/schemas.py — Doctor Review Request & Response Models
==================================================================

THREE ENDPOINTS
---------------
POST  /api/v1/cases/{case_id}/review   → Doctor creates the review
PATCH /api/v1/cases/{case_id}/review   → Doctor updates the review
GET   /api/v1/cases/{case_id}/review   → Patient or doctor reads the review

REVIEW LIFECYCLE
----------------
1. Doctor scans QR → assigned to case
2. Doctor opens case review screen
3. POST /review → creates DoctorReview row (status: in_progress)
4. Doctor fills in diagnosis + notes + treatment plan
5. PATCH /review → updates fields (status: completed when done)
6. Patient reads GET /review to see the outcome

STATUS TRANSITIONS
------------------
pending     → in_progress  (on first POST — doctor has started reviewing)
in_progress → completed    (when doctor submits confirmed_diagnosis)

Only the doctor who owns the review can update it.
The patient (and the assigned doctor) can read it.

CLINICAL STATUS
---------------
When a doctor completes the review, they also set the case's clinical_status:
active | follow_up_available | monitoring | resolved

This is what appears as the coloured badge in the patient's History screen.
"""

from datetime import datetime

from pydantic import BaseModel

from src.models.case import ClinicalStatus
from src.models.doctor_review import ReviewStatus


class CreateReviewRequest(BaseModel):
    """Body for POST /review — doctor starts their review of the case."""
    confirmed_diagnosis: str | None = None
    review_notes: str | None = None
    treatment_plan_json: str | None = None
    review_status: ReviewStatus = ReviewStatus.IN_PROGRESS


class UpdateReviewRequest(BaseModel):
    """
    Body for PATCH /review — all fields are optional.

    When review_status is set to COMPLETED:
    - reviewed_at timestamp is recorded
    - clinical_status on the case is updated (if provided)
    """
    confirmed_diagnosis: str | None = None
    review_notes: str | None = None
    treatment_plan_json: str | None = None
    review_status: ReviewStatus | None = None
    clinical_status: ClinicalStatus | None = None  # Updates the case, not the review


class DoctorReviewResponse(BaseModel):
    """Returned for GET and after POST/PATCH."""
    id: str
    case_id: str
    doctor_id: str
    confirmed_diagnosis: str | None
    review_notes: str | None
    treatment_plan_json: str | None
    review_status: ReviewStatus
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime
