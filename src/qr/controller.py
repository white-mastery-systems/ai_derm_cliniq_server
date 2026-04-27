"""
qr/controller.py — QR Code HTTP Endpoints
==========================================

Routes prefixed /api/v1/qr (set in src/api.py).

ROUTES
------
POST  /generate         → Patient: generate a single-use QR token for a case
POST  /scan/{token}     → Doctor: scan the QR token → get case access

WHY TWO SEPARATE PREFIXES?
---------------------------
/qr is a top-level prefix (not nested under /cases) because:
- QR generation requires a case_id in the *body* (not path) — Flutter's
  QR flow calls this after completing the chat, passing the case_id it knows.
- The scan endpoint receives just a token string — it doesn't know (and
  shouldn't need to know) the case_id up front. The server resolves it.

ROLES
-----
- generate: PATIENT only (it's their case, their QR to show)
- scan:     DOCTOR only (doctor scans QR with their device)
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import require_doctor, require_patient
from src.database.core import get_async_session
from src.models.user import User
from src.qr import service
from src.qr.schemas import GenerateQRRequest, PatientCodeAccessResponse, QRScanResponse, QRTokenResponse

router = APIRouter()


@router.post(
    "/generate",
    response_model=QRTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a QR token for a case (patient only)",
)
async def generate_qr(
    request: GenerateQRRequest,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> QRTokenResponse:
    """
    Create a single-use, expiring QR token for the given case.

    The Flutter app encodes the returned `token` string into a QR image
    (using the `qr_flutter` package), then displays it so the doctor can scan.

    Returns 201 with the token, expiry, and the full QR URL.
    Returns 400 if AI analysis is not yet complete for the case.
    Returns 404 if the case doesn't belong to this patient.
    """
    return await service.generate_qr(db, patient, request)


@router.post(
    "/scan/{token}",
    response_model=QRScanResponse,
    status_code=status.HTTP_200_OK,
    summary="Scan a QR token (doctor only)",
)
async def scan_qr(
    token: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> QRScanResponse:
    """
    Validate a QR token and return the associated case_id.

    On success:
    - Marks the token as used (single-use enforcement)
    - Auto-assigns the scanning doctor to the case (if not already assigned)
    - Returns case_id + patient name so the doctor app can navigate to the case

    Returns 410 Gone if the token is expired, already used, or not found.
    """
    return await service.scan_qr(db, doctor, token)


@router.post(
    "/by-display-id/{display_id}",
    response_model=PatientCodeAccessResponse,
    status_code=status.HTTP_200_OK,
    summary="Access a specific case via its display ID (doctor only)",
)
async def access_by_display_id(
    display_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> PatientCodeAccessResponse:
    """
    Alternative to QR scan — doctor types the case ID shown on the patient's
    QR screen (e.g. "AI-9135") instead of scanning.

    Looks up that specific case, auto-assigns the doctor, and returns case_id
    so the Flutter app navigates to the case review screen.

    Returns 400 if the format is invalid (must be AI-XXXX).
    Returns 404 if no case exists with that ID.
    Returns 400 if the case's AI analysis is not yet complete.
    """
    return await service.access_by_display_id(db, doctor, display_id)
