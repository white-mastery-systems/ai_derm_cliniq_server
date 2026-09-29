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
from src.core.email import (
    render_visit_email,
    render_red_flag_patient_email,
    render_red_flag_admin_email,
    send_email,
)
from src.logger import get_logger
from src.models.case import Case
from src.models.user import User, UserRole
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


async def _load_case_and_patient(case_id: str) -> tuple[str, str, str] | None:
    """
    Load patient email, full_name, and case display_id for a case.
    Returns (email, full_name, display_id) or None if case/patient not found.
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
            display_id = f"AI-{case.case_number}" if case.case_number else case_id[:8].upper()
            return case.patient.email, case.patient.full_name, display_id
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

    patient_email, patient_name, display_id = result

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
        display_id=display_id,
        patient_url=patient_url,
    )

    # 5. Build attachments list
    attachments = []
    if qr_png:
        attachments.append(("visit_qr.png", qr_png, "image/png"))

    # 6. Send
    subject = f"Your AiDerm Cliniq Visit Summary — Case {display_id}"
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


# ------------------------------------------------------------------ #
# Red Flag Emails — patient urgent alert + admin notification
# ------------------------------------------------------------------ #

async def _load_red_flag_recipients(case_id: str) -> tuple[str, str, str] | None:
    """
    Load patient email, patient name, and display_id for the case.
    Returns (patient_email, patient_name, display_id) or None.
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
            display_id = f"AI-{case.case_number}" if case.case_number else case_id[:8].upper()
            return case.patient.email, case.patient.full_name, display_id
    finally:
        await engine.dispose()


async def _load_admin_emails() -> list[str]:
    """Return email addresses of all active admin users."""
    engine = _make_engine()
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            result = await session.execute(
                select(User.email).where(
                    User.role == UserRole.ADMIN,
                    User.is_active.is_(True),
                )
            )
            return [row[0] for row in result.fetchall()]
    finally:
        await engine.dispose()


@celery_app.task(
    name="src.workers.tasks.email.send_red_flag_emails_task",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=60,
    soft_time_limit=50,
)
def send_red_flag_emails_task(
    self,
    case_id: str,
    display_id: str,
    flags: list[str],
    advice: str | None,
) -> dict:
    """
    Send red flag alert emails when a case is flagged as high-risk.

    - Patient receives an urgent email listing the concerns and advice.
    - All admin accounts receive a summary email for immediate review.
    """
    logger.info("red_flag_email_task_start", case_id=case_id)

    result = _run_async(_load_red_flag_recipients(case_id))
    if result is None:
        logger.error("red_flag_email_case_not_found", case_id=case_id)
        return {"status": "failed", "reason": "case_not_found"}

    patient_email, patient_name, display_id_loaded = result
    # Prefer the passed display_id (already computed by caller)
    effective_display_id = display_id or display_id_loaded

    sent_count = 0

    # 1. Patient email
    patient_html = render_red_flag_patient_email(
        patient_name=patient_name,
        display_id=effective_display_id,
        flags=flags,
        advice=advice,
    )
    if send_email(
        to_email=patient_email,
        subject=f"Urgent: Your AiDerm Cliniq Case {effective_display_id} Has Been Flagged",
        html_body=patient_html,
    ):
        sent_count += 1
        logger.info("red_flag_patient_email_sent", to=patient_email, case_id=case_id)
    else:
        logger.warning("red_flag_patient_email_failed", to=patient_email, case_id=case_id)

    # 2. Admin emails
    admin_emails = _run_async(_load_admin_emails())
    admin_html = render_red_flag_admin_email(
        patient_name=patient_name,
        display_id=effective_display_id,
        flags=flags,
        advice=advice,
    )
    for admin_email in admin_emails:
        if send_email(
            to_email=admin_email,
            subject=f"Red Flag Alert: Case {effective_display_id} Requires Urgent Review",
            html_body=admin_html,
        ):
            sent_count += 1
            logger.info("red_flag_admin_email_sent", to=admin_email, case_id=case_id)
        else:
            logger.warning("red_flag_admin_email_failed", to=admin_email, case_id=case_id)

    logger.info("red_flag_email_task_done", case_id=case_id, sent=sent_count)
    return {"status": "done", "sent": sent_count}
