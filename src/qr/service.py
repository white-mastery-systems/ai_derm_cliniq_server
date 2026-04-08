"""
qr/service.py — QR Token Business Logic
=========================================

Two operations:
1. generate_qr  — create a single-use, expiring token for a case
2. scan_qr      — validate the token, auto-assign doctor, return case_id

GENERATE GUARDS
---------------
- Case must exist and patient must own it
- ai_status must be COMPLETED (no point getting doctor before AI is done)
- Patient is shown a new QR even if previous tokens still exist (they share a case)

SCAN GUARDS
-----------
- Token must exist (404 if not)
- Token must not be expired (410 Gone)
- Token must not already be used (410 Gone)
- Doctor must be authenticated (any verified doctor can scan)
- On success: mark token used, auto-assign doctor to case if not already assigned

AUTO-ASSIGN ON SCAN
-------------------
Scanning the QR assigns the doctor to the case automatically (same logic as
PATCH /cases/{id}/assign). If the case is already assigned to a different doctor,
we still allow scan (the doctor gets the case info) but we don't overwrite
the existing assignment — the original doctor retains ownership.

WHY AUTO-ASSIGN ON SCAN?
-------------------------
The physical QR-scan flow means the doctor is right in front of the patient.
Requiring a separate assignment step after scan would be redundant UX.
"""

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.config import settings
from src.exceptions import (
    BadRequestException,
    CaseNotFoundException,
    ForbiddenException,
    QRTokenExpiredException,
)
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import AiStatus, Case
from src.models.qr_token import QRToken
from src.models.user import User, UserRole
from src.qr.schemas import GenerateQRRequest, QRScanResponse, QRTokenResponse
from src.workers.tasks.email import send_visit_email_task

logger = get_logger(__name__)

# URL base for the QR payload — doctor app scans this URL
_QR_BASE_URL = "/api/v1/qr/scan"


async def generate_qr(
    db: AsyncSession,
    patient: User,
    request: GenerateQRRequest,
) -> QRTokenResponse:
    """
    Generate a single-use QR token for a case.

    The returned token string is what the Flutter app encodes into a QR image.
    """
    if patient.role != UserRole.PATIENT:
        raise ForbiddenException(message="Only patients can generate QR codes")

    # Load case and verify ownership
    result = await db.execute(select(Case).where(Case.id == request.case_id))
    case = result.scalar_one_or_none()
    if case is None or case.patient_id != patient.id:
        raise CaseNotFoundException(message=f"No case found with id: {request.case_id}")

    # Guard: AI analysis must be done before showing the QR
    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before generating a QR code. "
                    f"Current status: {case.ai_status.value}"
        )

    # Create token
    token_value = secrets.token_urlsafe(32)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(
        hours=settings.QR_TOKEN_EXPIRE_HOURS
    )

    qr_token = QRToken(
        id=new_uuid(),
        case_id=request.case_id,
        token=token_value,
        expires_at=expires_at,
        used=False,
    )
    db.add(qr_token)

    logger.info("qr_generated", case_id=request.case_id, expires_at=expires_at)

    # Fire-and-forget: send visit summary email with QR code attachment
    try:
        send_visit_email_task.delay(request.case_id, token_value)
    except Exception as exc:
        # Redis may be unavailable in dev — log and continue, never block QR generation
        logger.warning("email_task_enqueue_failed", case_id=request.case_id, error=str(exc))

    return QRTokenResponse(
        token=token_value,
        case_id=request.case_id,
        display_id=f"AI-{case.case_number}" if case.case_number else None,
        expires_at=expires_at,
        qr_url=f"{_QR_BASE_URL}/{token_value}",
    )


async def scan_qr(
    db: AsyncSession,
    doctor: User,
    token_value: str,
) -> QRScanResponse:
    """
    Validate a QR token and return the associated case_id.

    On success:
    - Marks the token as used (single-use enforcement)
    - Auto-assigns the doctor to the case if not already assigned
    """
    if doctor.role != UserRole.DOCTOR:
        raise ForbiddenException(message="Only doctors can scan QR codes")

    # Load token with case + patient eagerly
    result = await db.execute(
        select(QRToken)
        .where(QRToken.token == token_value)
        .options(
            selectinload(QRToken.case).selectinload(Case.patient)
        )
    )
    qr_token = result.scalar_one_or_none()

    if qr_token is None:
        raise QRTokenExpiredException(message="QR code is invalid or has expired")

    # Normalise naive datetime from SQLite
    expires_at = qr_token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < datetime.now(tz=timezone.utc):
        raise QRTokenExpiredException(message="This QR code has expired. Ask the patient to generate a new one.")

    if qr_token.used:
        raise QRTokenExpiredException(message="This QR code has already been used.")

    case = qr_token.case
    patient = case.patient

    # Mark token as used
    qr_token.used = True
    qr_token.used_at = datetime.now(tz=timezone.utc)

    # Auto-assign doctor to case (only if not already assigned to another)
    if case.doctor_id is None:
        case.doctor_id = doctor.id
        logger.info("doctor_auto_assigned_via_qr", case_id=case.id, doctor_id=doctor.id)
    elif case.doctor_id != doctor.id:
        # Different doctor scanned — still valid scan, just don't overwrite assignment
        logger.info(
            "qr_scan_case_already_assigned",
            case_id=case.id,
            existing_doctor=case.doctor_id,
            scanning_doctor=doctor.id,
        )

    logger.info("qr_scanned", case_id=case.id, doctor_id=doctor.id)

    return QRScanResponse(
        case_id=case.id,
        patient_name=patient.full_name,
    )
