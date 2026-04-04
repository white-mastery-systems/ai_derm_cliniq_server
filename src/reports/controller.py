"""
reports/controller.py — Report HTTP Endpoints
==============================================

All routes nested under /api/v1/cases/{case_id} (set in src/api.py).

ROUTES
------
POST  /report  → Doctor triggers PDF generation (202 Accepted)
GET   /report  → Patient or doctor fetches report + signed download URL (200)

ROLES
-----
POST: DOCTOR only (and must be assigned to the case)
GET:  PATIENT (owns the case) or DOCTOR (assigned to the case)
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_doctor
from src.database.core import get_async_session
from src.models.user import User
from src.reports import service
from src.reports.schemas import ReportResponse, ReportTriggerResponse

router = APIRouter()


@router.post(
    "/report",
    response_model=ReportTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger PDF report generation (doctor only)",
)
async def trigger_report(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> ReportTriggerResponse:
    """
    Enqueue PDF report generation for a completed case.

    The doctor review must have review_status=completed before calling this.
    Returns 202 immediately — poll GET /report to check when the PDF is ready.

    Returns 400 if the review is not yet completed.
    Returns 409 if a report already exists.
    """
    return await service.trigger_report(db, doctor, case_id)


@router.get(
    "/report",
    response_model=ReportResponse,
    status_code=status.HTTP_200_OK,
    summary="Get the generated report (patient or doctor)",
)
async def get_report(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> ReportResponse:
    """
    Fetch the generated PDF report for a case.

    Returns a signed GCS download URL (valid 30 minutes).
    Returns 404 if the report has not been generated yet.
    """
    return await service.get_report(db, user, case_id)
