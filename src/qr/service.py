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
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
from src.qr.schemas import GenerateQRRequest, PatientCodeAccessResponse, QRScanResponse, QRTokenResponse
from src.workers.tasks.email import send_visit_email_task
from src.workers.tasks.notifications import notify_patient_doctor_assigned

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

    # Return existing unused QR for this case (one-per-case rule)
    existing = await db.execute(
        select(QRToken)
        .where(QRToken.case_id == request.case_id, QRToken.used == False)  # noqa: E712
        .order_by(QRToken.created_at.desc())
        .limit(1)
    )
    qr_token = existing.scalar_one_or_none()

    if qr_token is None:
        # No active QR exists — create one that never expires
        _NEVER = datetime(9999, 12, 31, tzinfo=timezone.utc)
        qr_token = QRToken(
            id=new_uuid(),
            case_id=request.case_id,
            token=secrets.token_urlsafe(32),
            expires_at=_NEVER,
            used=False,
        )
        db.add(qr_token)

    logger.info("qr_generated", case_id=request.case_id, reused=qr_token.id is not None)

    # Fire-and-forget: send visit summary email with QR code attachment
    try:
        send_visit_email_task.delay(request.case_id, qr_token.token)
    except Exception as exc:
        logger.warning("email_task_enqueue_failed", case_id=request.case_id, error=str(exc))

    return QRTokenResponse(
        token=qr_token.token,
        case_id=request.case_id,
        display_id=f"AI-{case.case_number}" if case.case_number else None,
        expires_at=qr_token.expires_at,
        qr_url=f"{_QR_BASE_URL}/{qr_token.token}",
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
    if doctor.role not in (UserRole.DOCTOR, UserRole.ADMIN):
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

    if qr_token.used:
        raise QRTokenExpiredException(message="This QR code has already been used.")

    case = qr_token.case
    patient = case.patient

    # Mark token as used
    qr_token.used = True
    qr_token.used_at = datetime.now(tz=timezone.utc)

    # Auto-assign doctor to case (only if not already assigned to another)
    newly_assigned = False
    if case.doctor_id is None:
        case.doctor_id = doctor.id
        newly_assigned = True
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

    if newly_assigned:
        import json as _json
        from src.models.audit_log import AuditEventType, CaseAuditLog
        db.add(CaseAuditLog(
            case_id=case.id,
            event_type=AuditEventType.DOCTOR_ASSIGNED,
            actor_id=doctor.id,
            actor_role=doctor.role.value,
            event_data=_json.dumps({"method": "qr_scan", "doctor_name": doctor.full_name}),
        ))

    if newly_assigned:
        display_id = f"AI-{case.case_number}" if case.case_number else case.id[:8].upper()
        try:
            notify_patient_doctor_assigned.delay(
                patient.id, case.id, display_id, doctor.full_name
            )
        except Exception as exc:
            logger.warning("notify_doctor_assigned_enqueue_failed", case_id=case.id, error=str(exc))

    return QRScanResponse(
        case_id=case.id,
        patient_name=patient.full_name,
    )


async def access_by_display_id(
    db: AsyncSession,
    doctor: User,
    display_id: str,
) -> PatientCodeAccessResponse:
    """
    Grant a doctor access to a specific case via its display ID (e.g. "AI-9135").

    Same outcome as scan_qr() — doctor is auto-assigned, case_id returned —
    but the entry method is the human-readable case ID shown on the patient's
    QR screen instead of scanning the QR token.
    """
    if doctor.role not in (UserRole.DOCTOR, UserRole.ADMIN):
        raise ForbiddenException(message="Only doctors can access cases via case ID")

    # Parse "AI-9135" → 9135
    normalized = display_id.strip().upper()
    if not normalized.startswith("AI-") or not normalized[3:].isdigit():
        raise BadRequestException(
            message="Invalid case ID format. Expected format: AI-XXXX (e.g. AI-9135)"
        )
    case_number = int(normalized[3:])

    # Look up the specific case by its sequential number
    result = await db.execute(
        select(Case)
        .where(Case.case_number == case_number)
        .options(selectinload(Case.patient))
    )
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with ID: {normalized}")

    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="This case's AI analysis is not yet complete. "
                    "Ask the patient to finish their consultation first."
        )

    # Auto-assign doctor (same logic as QR scan)
    newly_assigned = False
    if case.doctor_id is None:
        case.doctor_id = doctor.id
        newly_assigned = True
        logger.info("doctor_auto_assigned_via_display_id", case_id=case.id, doctor_id=doctor.id)
    elif case.doctor_id != doctor.id:
        logger.info(
            "display_id_access_case_already_assigned",
            case_id=case.id,
            existing_doctor=case.doctor_id,
            scanning_doctor=doctor.id,
        )

    logger.info("display_id_access", case_id=case.id, doctor_id=doctor.id, display_id=normalized)

    if newly_assigned:
        import json as _json
        from src.models.audit_log import AuditEventType, CaseAuditLog
        db.add(CaseAuditLog(
            case_id=case.id,
            event_type=AuditEventType.DOCTOR_ASSIGNED,
            actor_id=doctor.id,
            actor_role=doctor.role.value,
            event_data=_json.dumps({"method": "display_id", "doctor_name": doctor.full_name}),
        ))

    if newly_assigned:
        try:
            notify_patient_doctor_assigned.delay(
                case.patient.id, case.id, normalized, doctor.full_name
            )
        except Exception as exc:
            logger.warning("notify_doctor_assigned_enqueue_failed", case_id=case.id, error=str(exc))

    return PatientCodeAccessResponse(
        case_id=case.id,
        patient_name=case.patient.full_name,
        message="Access granted via case ID.",
    )
