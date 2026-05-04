"""
workers/tasks/notifications.py — Push Notification Celery Tasks
===============================================================

All 7 notification events are dispatched as fire-and-forget Celery tasks
so that the triggering API handler is never blocked by FCM network calls.

TASKS
-----
notify_admins_doctor_registered   — new doctor needs approval (→ all admins)
notify_doctor_approved            — doctor account approved (→ doctor)
notify_doctor_rejected            — doctor account rejected (→ doctor)
notify_patient_ai_complete        — AI analysis done, show QR (→ patient)
notify_patient_review_complete    — doctor submitted review (→ patient)
notify_patient_status_update      — case clinical_status changed (→ patient)
notify_admins_red_flag            — case flagged as high-risk (→ all admins)

DB ACCESS PATTERN
-----------------
Same as email.py: NullPool engine + asyncio.run() inside the sync Celery task.
FCM calls are synchronous (firebase-admin SDK), so no second event loop is needed.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.config import settings
from src.logger import get_logger
from src.models.user import User, UserRole
from src.notifications.service import send_push_multicast, send_push_notification
from src.workers.celery_app import celery_app

logger = get_logger(__name__)


def _make_engine():
    return create_async_engine(settings.DATABASE_URL, poolclass=NullPool)


def _run_async(coro):
    return asyncio.run(coro)


async def _get_admin_tokens() -> list[str]:
    """Return FCM tokens for all active admins."""
    engine = _make_engine()
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            result = await session.execute(
                select(User.fcm_token).where(
                    User.role == UserRole.ADMIN,
                    User.is_active == True,  # noqa: E712
                    User.fcm_token.isnot(None),
                )
            )
            return [row[0] for row in result.all()]
    finally:
        await engine.dispose()


async def _get_user_token(user_id: str) -> str | None:
    """Return the FCM token for a single user, or None if not set."""
    engine = _make_engine()
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            result = await session.execute(
                select(User.fcm_token).where(User.id == user_id)
            )
            row = result.scalar_one_or_none()
            return row
    finally:
        await engine.dispose()


# ================================================================== #
# 1. Doctor registers → notify all admins
# ================================================================== #

@celery_app.task(
    name="src.workers.tasks.notifications.notify_admins_doctor_registered",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=30,
    soft_time_limit=25,
)
def notify_admins_doctor_registered(self, doctor_name: str, doctor_id: str) -> dict:
    """Send push notification to all admins when a new doctor registers."""
    logger.info("notify_admins_doctor_registered_start", doctor_id=doctor_id)
    try:
        tokens = _run_async(_get_admin_tokens())
        count = send_push_multicast(
            tokens=tokens,
            title="New Doctor Registration",
            body=f"Dr. {doctor_name} has registered and is pending approval.",
            data={"type": "doctor_pending", "user_id": doctor_id},
        )
        logger.info("notify_admins_doctor_registered_done", sent=count)
        return {"status": "sent", "count": count}
    except Exception as exc:
        logger.warning("notify_admins_doctor_registered_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


# ================================================================== #
# 2. Admin approves doctor → notify that doctor
# ================================================================== #

@celery_app.task(
    name="src.workers.tasks.notifications.notify_doctor_approved",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=30,
    soft_time_limit=25,
)
def notify_doctor_approved(self, doctor_id: str) -> dict:
    """Notify the doctor that their account has been approved."""
    logger.info("notify_doctor_approved_start", doctor_id=doctor_id)
    try:
        token = _run_async(_get_user_token(doctor_id))
        if not token:
            return {"status": "skipped", "reason": "no_fcm_token"}
        ok = send_push_notification(
            token=token,
            title="Account Approved",
            body="Your AiDerm Cliniq account has been approved. You can now start seeing patients.",
            data={"type": "account_approved"},
        )
        return {"status": "sent" if ok else "failed"}
    except Exception as exc:
        logger.warning("notify_doctor_approved_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


# ================================================================== #
# 3. Admin rejects doctor → notify that doctor
# ================================================================== #

@celery_app.task(
    name="src.workers.tasks.notifications.notify_doctor_rejected",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=30,
    soft_time_limit=25,
)
def notify_doctor_rejected(self, doctor_id: str) -> dict:
    """Notify the doctor that their account was not approved."""
    logger.info("notify_doctor_rejected_start", doctor_id=doctor_id)
    try:
        token = _run_async(_get_user_token(doctor_id))
        if not token:
            return {"status": "skipped", "reason": "no_fcm_token"}
        ok = send_push_notification(
            token=token,
            title="Account Not Approved",
            body="Your AiDerm Cliniq registration was not approved. Contact support for details.",
            data={"type": "account_rejected"},
        )
        return {"status": "sent" if ok else "failed"}
    except Exception as exc:
        logger.warning("notify_doctor_rejected_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


# ================================================================== #
# 4. AI analysis completes → notify patient
# ================================================================== #

@celery_app.task(
    name="src.workers.tasks.notifications.notify_patient_ai_complete",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=30,
    soft_time_limit=25,
)
def notify_patient_ai_complete(self, patient_id: str, case_id: str, display_id: str) -> dict:
    """Notify the patient that their AI analysis is ready."""
    logger.info("notify_patient_ai_complete_start", patient_id=patient_id, case_id=case_id)
    try:
        token = _run_async(_get_user_token(patient_id))
        if not token:
            return {"status": "skipped", "reason": "no_fcm_token"}
        ok = send_push_notification(
            token=token,
            title="Your Analysis is Ready",
            body=f"Case {display_id} analysis is complete. Show your QR code to your doctor.",
            data={"type": "ai_complete", "case_id": case_id},
        )
        return {"status": "sent" if ok else "failed"}
    except Exception as exc:
        logger.warning("notify_patient_ai_complete_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


# ================================================================== #
# 5. Doctor submits review → notify patient
# ================================================================== #

@celery_app.task(
    name="src.workers.tasks.notifications.notify_patient_review_complete",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=30,
    soft_time_limit=25,
)
def notify_patient_review_complete(
    self, patient_id: str, case_id: str, display_id: str, doctor_name: str
) -> dict:
    """Notify the patient that the doctor submitted their review."""
    logger.info("notify_patient_review_complete_start", patient_id=patient_id, case_id=case_id)
    try:
        token = _run_async(_get_user_token(patient_id))
        if not token:
            return {"status": "skipped", "reason": "no_fcm_token"}
        ok = send_push_notification(
            token=token,
            title="Doctor Review Ready",
            body=f"Dr. {doctor_name} has reviewed your case {display_id}. Tap to see your results.",
            data={"type": "review_complete", "case_id": case_id},
        )
        return {"status": "sent" if ok else "failed"}
    except Exception as exc:
        logger.warning("notify_patient_review_complete_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


# ================================================================== #
# 6. Case clinical status updated → notify patient
# ================================================================== #

@celery_app.task(
    name="src.workers.tasks.notifications.notify_patient_status_update",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=30,
    soft_time_limit=25,
)
def notify_patient_status_update(
    self, patient_id: str, case_id: str, display_id: str, new_status: str
) -> dict:
    """Notify the patient when their case clinical status changes."""
    logger.info("notify_patient_status_update_start", patient_id=patient_id, case_id=case_id)

    # Human-readable labels for clinical_status enum values
    status_labels = {
        "active": "Active",
        "follow_up_available": "Follow-up Available",
        "monitoring": "Monitoring",
        "resolved": "Resolved",
    }
    label = status_labels.get(new_status, new_status.replace("_", " ").title())

    try:
        token = _run_async(_get_user_token(patient_id))
        if not token:
            return {"status": "skipped", "reason": "no_fcm_token"}
        ok = send_push_notification(
            token=token,
            title="Case Status Updated",
            body=f"Your case {display_id} status is now: {label}.",
            data={"type": "status_update", "case_id": case_id, "status": new_status},
        )
        return {"status": "sent" if ok else "failed"}
    except Exception as exc:
        logger.warning("notify_patient_status_update_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}


# ================================================================== #
# 7. Red flag detected → notify all admins
# ================================================================== #

@celery_app.task(
    name="src.workers.tasks.notifications.notify_admins_red_flag",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    time_limit=30,
    soft_time_limit=25,
)
def notify_admins_red_flag(self, case_id: str, display_id: str) -> dict:
    """Notify all admins when a case is flagged as high-risk."""
    logger.info("notify_admins_red_flag_start", case_id=case_id)
    try:
        tokens = _run_async(_get_admin_tokens())
        count = send_push_multicast(
            tokens=tokens,
            title="High-Risk Case Flagged",
            body=f"Case {display_id} has been flagged for urgent review.",
            data={"type": "red_flag", "case_id": case_id},
        )
        logger.info("notify_admins_red_flag_done", sent=count)
        return {"status": "sent", "count": count}
    except Exception as exc:
        logger.warning("notify_admins_red_flag_failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}
