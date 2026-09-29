"""
reports/schemas.py — Report Request & Response Models
======================================================

TWO ENDPOINTS
-------------
POST /api/v1/cases/{case_id}/report   → Doctor triggers PDF generation (202)
GET  /api/v1/cases/{case_id}/report   → Patient or doctor fetches report + download URL

REPORT LIFECYCLE
----------------
1. Doctor completes review (review_status=completed)
2. POST /report → 202 { task_id } — Celery generates PDF in background
3. Flutter polls GET /report → 404 while pending, then 200 when ready
4. GET increments download_count each time (tracks access)

DOWNLOAD URL
------------
The report PDF is stored in GCS (private bucket).
GET /report generates a signed URL valid for 30 minutes.
Flutter opens this URL in a WebView or PDF viewer.
"""

from datetime import datetime

from pydantic import BaseModel

from src.models.case_report import ReportType


class ReportTriggerResponse(BaseModel):
    """Returned immediately when POST /report is accepted (202)."""
    message: str = "Report generation started. Poll GET /report to check when ready."
    case_id: str
    task_id: str


class ReportResponse(BaseModel):
    """Returned when the report is ready (GET /report → 200)."""
    id: str
    case_id: str
    report_type: ReportType
    generated_at: datetime
    download_url: str    # GCS signed URL, valid for 30 minutes
    download_count: int


class ReportListItem(BaseModel):
    """
    One entry in the doctor's Reports tab list.

    Includes enough case context (patient name, case number, date) for the
    Flutter list tile — no extra request needed per item.
    """
    id: str
    case_id: str
    case_number: int        # e.g. 9001  (shown as "AI-9001" in the UI)
    patient_name: str
    report_type: ReportType
    generated_at: datetime
    download_url: str       # GCS signed URL, valid for 30 minutes
    download_count: int


class ReportListResponse(BaseModel):
    """Returned by GET /api/v1/reports."""
    reports: list[ReportListItem]
    total: int
