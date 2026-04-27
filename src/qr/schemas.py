"""
qr/schemas.py — QR Token Request & Response Models
====================================================

TWO ENDPOINTS
-------------
POST /api/v1/qr/generate        → Patient generates a QR token for their case
POST /api/v1/qr/scan/{token}    → Doctor scans the QR → gets case_id + access

QR FLOW (from Figma Basic Patient Flow)
----------------------------------------
1. Patient completes Q&A (is_complete=True in GET /chat)
2. Patient taps "Show QR Code" → POST /qr/generate
3. Backend creates QRToken row (single-use, 24h expiry)
4. Backend returns token string → Flutter renders QR image (qr_flutter package)
5. Patient shows phone to doctor
6. Doctor scans QR → POST /qr/scan/{token}
7. Backend validates: not expired, not used → returns case_id
8. Doctor app navigates to case review screen; doctor auto-assigned to case

WHY OPAQUE TOKEN, NOT CASE_ID IN QR?
--------------------------------------
Embedding case_id directly in the QR would let anyone who photographs the
QR code access the case permanently. An opaque token:
- Is meaningless without the backend
- Expires after QR_TOKEN_EXPIRE_HOURS (default 24h)
- Can only be used once (single-use guard)
"""

from datetime import datetime

from pydantic import BaseModel


class GenerateQRRequest(BaseModel):
    case_id: str


class QRTokenResponse(BaseModel):
    """Returned after successful QR generation."""
    token: str
    case_id: str
    display_id: str | None = None   # "AI-9021" — shown below the QR image on Assessment Complete screen
    expires_at: datetime | None = None
    qr_url: str     # Full URL encoded in the QR: /api/v1/qr/scan/{token}


class QRScanResponse(BaseModel):
    """
    Returned when a doctor scans a valid, unused, non-expired QR token.
    The doctor's app uses case_id to navigate to the case review screen.
    """
    case_id: str
    patient_name: str
    message: str = "QR code verified. You have been granted access to this case."


class PatientCodeAccessResponse(BaseModel):
    """
    Returned when a doctor accesses a case via its display ID (e.g. "AI-9135").
    Same shape as QRScanResponse so the Flutter app handles both identically.
    """
    case_id: str
    patient_name: str
    message: str = "Access granted via case ID."
