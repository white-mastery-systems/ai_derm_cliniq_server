"""
workers/tasks/email.py — Visit Summary Email Celery Task
=========================================================

Single task: send_visit_email_task(case_id, token)

Triggered automatically after a patient generates a QR code.
Sends a visit summary email to the patient containing:
  - Their case number
  - A link to view their case in the Flutter app
  - The doctor QR code image as an attachment

TASK FLOW
---------
1. Load case + patient profile from DB (need email + full_name)
2. Generate QR code image (PNG bytes) from the scan URL
3. Render HTML email via Jinja2 template (src/core/email.py)
4. Send via Gmail SMTP

FAILURE HANDLING
-----------------
Email is non-critical — if it fails, the QR was already generated and
the patient can still use it. The task logs the failure but never raises.
No case status is updated on email failure.

WHY A CELERY TASK?
-------------------
smtplib.SMTP_SSL blocks while connecting to Gmail (~1-3 seconds).
Running it inside the FastAPI request would slow down QR generation.
A background task keeps the endpoint response time fast (<100ms).

DB ACCESS
----------
Same pattern as other tasks: NullPool engine + asyncio.run().
"""

import asyncio
import io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from src.config import settings
from src.core.email import render_visit_email, send_email
from src.logger import get_logger
from src.models.case import Case
from src.workers.celery_app import celery_app

logger = get_logger(__name__)


def _make_engine():
    return create_async_engine(settings.DATABASE_URL, poolclass=NullPool)


def _run_async(coro):
    return asyncio.run(coro)


def _generate_qr_png(scan_url: str) -> bytes:
    """
    Generate a QR code PNG image for the given URL.
    Returns raw PNG bytes.

    Uses the `qrcode` library (already in requirements.txt).
    The QR encodes the full scan URL so the doctor app can navigate
    directly to the case on scan.
    """
    import qrcode

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(scan_url)
    qr.make(fit=True)

    # Uses Pillow (already in requirements.txt) to render PNG
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _load_case_and_patient(case_id: str) -> tuple[str, str] | None:
    """
    Load patient email and full_name for a case.
    Returns (email, full_name) or None if case/patient not found.
    """
    engine = _make_engine()
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            result = await session.execute(
                select(Case)
                .where(Case.id == case_id)
                .options(selectinload(Case.patient))
            )
            case = result.scalar_one_or_none()
            if case is None or case.patient is None:
                return None
            return case.patient.email, case.patient.full_name
    finally:
        await engine.dispose()


@celery_app.task(
    name="src.workers.tasks.email.send_visit_email_task",
    bind=True,
    max_retries=2,
    default_retry_delay=60,   # retry after 60s if SMTP is temporarily down
    time_limit=60,
    soft_time_limit=50,
)
def send_visit_email_task(self, case_id: str, token: str) -> dict:
    """
    Send a visit summary email to the patient with their QR code attached.

    Parameters
    ----------
    case_id : str   — UUID of the case
    token   : str   — QR token value (encoded in the QR image)

    Returns a dict with send status for Celery result backend.
    """
    logger.info("email_task_started", case_id=case_id)

    # 1. Load patient contact info
    result = _run_async(_load_case_and_patient(case_id))
    if result is None:
        logger.error("email_task_case_not_found", case_id=case_id)
        return {"status": "failed", "reason": "case_not_found", "case_id": case_id}

    patient_email, patient_name = result

    # 2. Build URLs
    scan_url = f"{settings.FRONTEND_URL}/api/v1/qr/scan/{token}"
    patient_url = f"{settings.FRONTEND_URL}/case/{case_id}"

    # 3. Generate QR PNG
    try:
        qr_png = _generate_qr_png(scan_url)
    except Exception as exc:
        logger.error("email_task_qr_generation_failed", case_id=case_id, error=str(exc))
        qr_png = None   # Still send the email — just without the QR attachment

    # 4. Render HTML body
    html_body = render_visit_email(
        patient_name=patient_name,
        case_id=case_id,
        patient_url=patient_url,
    )

    # 5. Build attachments list
    attachments = []
    if qr_png:
        attachments.append(("visit_qr.png", qr_png, "image/png"))

    # 6. Send
    subject = f"Your AiDerm Cliniq Visit Summary — Case #{case_id[:8].upper()}"
    success = send_email(
        to_email=patient_email,
        subject=subject,
        html_body=html_body,
        attachments=attachments,
    )

    if success:
        logger.info("email_task_completed", case_id=case_id, to=patient_email)
        return {"status": "sent", "to": patient_email, "case_id": case_id}

    # Email failed — retry up to max_retries times for transient SMTP issues
    logger.warning("email_task_send_failed", case_id=case_id, to=patient_email)
    return {"status": "failed", "reason": "smtp_error", "case_id": case_id}
