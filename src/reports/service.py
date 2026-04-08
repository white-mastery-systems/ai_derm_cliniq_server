"""
reports/service.py — Report Generation Business Logic
======================================================

Two operations:
1. trigger_report  — enqueue PDF generation task (doctor only)
2. get_report      — fetch report metadata + signed download URL

TRIGGER GUARDS
--------------
- Doctor must be assigned to the case (case.doctor_id == doctor.id)
- DoctorReview must exist and have review_status=COMPLETED
  (no point generating a report before the doctor is done)
- CaseReport must not already exist — 409 if it does
  (prevents duplicate PDFs; delete + retry is a future feature)

GET ACCESS
----------
- The patient who owns the case can read the report
- The assigned doctor can read the report
- Others get 404 (case enumeration prevention)
- Returns 404 if the report has not been generated yet

DOWNLOAD URL
------------
Each GET call generates a fresh GCS signed URL (30-minute expiry)
and increments download_count. The Flutter app uses the URL to open
the PDF in a viewer or save it locally.

TASK IMPORT
-----------
generate_report_task is imported at module level so tests can patch it
(same pattern as Layers 5 & 6). Do NOT move it inside the function.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.exceptions import (
    BadRequestException,
    CaseNotFoundException,
    ConflictException,
    ForbiddenException,
    ReportNotFoundException,
)
from src.logger import get_logger
from src.models.case import Case
from src.models.doctor_review import ReviewStatus
from src.models.user import User, UserRole
from src.reports.schemas import ReportResponse, ReportTriggerResponse
from src.storage import gcs

# Module-level import — required for unittest.mock.patch in tests
from src.workers.tasks.reports import generate_report_task

logger = get_logger(__name__)


async def trigger_report(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> ReportTriggerResponse:
    """
    Enqueue PDF report generation for a case.

    Guards:
    - Doctor only
    - Doctor must be assigned to the case
    - Doctor review must be COMPLETED
    - Report must not already exist (409)

    Returns 202 Accepted with task_id immediately.
    """
    if doctor.role != UserRole.DOCTOR:
        raise ForbiddenException(message="Only doctors can generate reports")

    result = await db.execute(
        select(Case)
        .where(Case.id == case_id)
        .options(
            selectinload(Case.doctor_review),
            selectinload(Case.report),
        )
    )
    case = result.scalar_one_or_none()

    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if case.doctor_id != doctor.id:
        raise ForbiddenException(
            message="You are not assigned to this case. "
                    "Scan the patient's QR code to gain access."
        )

    if case.doctor_review is None or case.doctor_review.review_status != ReviewStatus.COMPLETED:
        raise BadRequestException(
            message="The doctor review must be completed before generating a report. "
                    "Set review_status=completed via PATCH /cases/{id}/review first."
        )

    if case.report is not None:
        raise ConflictException(
            message="A report already exists for this case. "
                    "Fetch it via GET /cases/{id}/report."
        )

    # Enqueue the Celery task
    result = generate_report_task.delay(case_id)

    logger.info("report_generation_triggered", case_id=case_id, task_id=result.id)

    return ReportTriggerResponse(
        case_id=case_id,
        task_id=result.id,
    )


async def get_report(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> ReportResponse:
    """
    Fetch the generated report for a case.

    Access: patient who owns the case, or the assigned doctor.
    Returns 404 if:
    - Case does not exist
    - User does not have access
    - Report has not been generated yet

    Increments download_count on each call.
    """
    result = await db.execute(
        select(Case)
        .where(Case.id == case_id)
        .options(selectinload(Case.report))
    )
    case = result.scalar_one_or_none()

    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    # Access control: patient who owns the case, assigned doctor, or admin
    if user.role != UserRole.ADMIN:
        if user.role == UserRole.PATIENT and case.patient_id != user.id:
            raise CaseNotFoundException(message=f"No case found with id: {case_id}")

        if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
            raise ForbiddenException(message="You are not assigned to this case.")

    if case.report is None:
        raise ReportNotFoundException(
            message="Report not yet generated. "
                    "Ask the assigned doctor to trigger it via POST /cases/{id}/report."
        )

    report = case.report

    # Generate signed URL for download
    download_url = gcs.get_signed_url(report.gcs_path, expiry_minutes=30)

    # Track access
    report.download_count += 1

    logger.info(
        "report_accessed",
        case_id=case_id,
        user_id=user.id,
        download_count=report.download_count,
    )

    return ReportResponse(
        id=report.id,
        case_id=report.case_id,
        report_type=report.report_type,
        generated_at=report.generated_at,
        download_url=download_url,
        download_count=report.download_count,
    )
