"""
workers/tasks/doctor_analysis.py — Doctor Diagnose Visual Findings Task
=======================================================================

TASK: generate_visual_findings_task(case_id)
--------------------------------------------
Runs the 3-step sequential image analysis for the doctor diagnose flow:
  Step 1 — clinical image(s)        → JSON clinical description
  Step 2 — dermoscopy image(s)      → JSON dermoscopic description (uses clinical as context)
  Step 3 — pathology image(s)       → JSON pathological description (uses both as context)

Result stored as Case.visual_findings JSON:
  {
    "clinical":   { ...structured fields... },
    "dermoscopy": { ...structured fields... },
    "pathology":  { ...structured fields... }
  }

Keys with no images uploaded get an empty dict {}.

TRIGGER
-------
POST /cases/{case_id}/visual-findings/generate  (DOCTOR only)
Service enqueues: generate_visual_findings_task.delay(case_id)

STATUS TRACKING
---------------
Uses Case.ai_status:
  PENDING     → PROCESSING (task starts)
  PROCESSING  → COMPLETED  (all steps done, visual_findings saved)
  PROCESSING  → FAILED     (any step raises unrecoverable error)

This reuses the existing ai_status enum — no new status columns needed.

DB ACCESS
---------
Celery workers are sync processes. asyncio.run() wraps every async query,
same pattern as analysis.py.
"""

import asyncio
import json

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.ai.llm_router import call_llm, extract_json
from src.ai.prompts.doctor_image_prompts import DoctorImageAnalysisPrompts
from src.config import settings
from src.logger import get_logger
from src.models.case import AiStatus, Case
from src.models.case_image import CaseImage, ImageType
from src.models.patient_profile import PatientProfile
from src.storage import gcs
from src.workers.celery_app import celery_app

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# DB helpers — same pattern as analysis.py
# ------------------------------------------------------------------ #

def _make_engine():
    return create_async_engine(settings.DATABASE_URL, poolclass=NullPool)


def _run_async(coro):
    return asyncio.run(coro)


async def _get_case(session: AsyncSession, case_id: str) -> Case | None:
    result = await session.execute(select(Case).where(Case.id == case_id))
    return result.scalar_one_or_none()


async def _get_images_by_type(
    session: AsyncSession, case_id: str, image_type: ImageType
) -> list[bytes]:
    """Download all GCS images of a given type for a case. Returns list of raw bytes."""
    result = await session.execute(
        select(CaseImage)
        .where(CaseImage.case_id == case_id, CaseImage.image_type == image_type)
        .order_by(CaseImage.upload_order)
    )
    images = list(result.scalars().all())
    image_bytes: list[bytes] = []
    for img in images:
        try:
            data = await asyncio.to_thread(gcs.download_bytes, img.gcs_path)
            image_bytes.append(data)
        except Exception as exc:
            logger.warning(
                "visual_findings_image_download_failed",
                case_id=case_id,
                image_id=img.id,
                error=str(exc),
            )
    return image_bytes


async def _get_personal_particulars(session: AsyncSession, patient_id: str) -> str:
    result = await session.execute(
        select(PatientProfile).where(PatientProfile.user_id == patient_id)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        return "Age: unknown, Sex: unknown"
    parts = []
    if profile.date_of_birth:
        from datetime import date
        age = (date.today() - profile.date_of_birth).days // 365
        parts.append(f"Age: {age}")
    if profile.gender:
        parts.append(f"Sex: {profile.gender}")
    return ", ".join(parts) if parts else "Age: unknown, Sex: unknown"


async def _fail_case(session: AsyncSession, case_id: str, reason: str) -> None:
    case = await _get_case(session, case_id)
    if case:
        case.ai_status = AiStatus.FAILED
        case.celery_task_id = None
        await session.commit()
    logger.warning("visual_findings_failed", case_id=case_id, reason=reason)


# ------------------------------------------------------------------ #
# Main Task
# ------------------------------------------------------------------ #

@celery_app.task(
    name="doctor_analysis.generate_visual_findings",
    bind=True,
    max_retries=1,
    time_limit=300,
    soft_time_limit=270,
)
def generate_visual_findings_task(self, case_id: str) -> None:
    """
    Generate visual findings for all 3 image types for a doctor-created case.

    Idempotent: if visual_findings already populated, exits immediately.
    Stores result in Case.visual_findings as JSON string.
    """
    logger.info("visual_findings_task_started", case_id=case_id)

    async def _run():
        engine = _make_engine()
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            case = await _get_case(session, case_id)
            if case is None:
                logger.error("visual_findings_case_not_found", case_id=case_id)
                return

            # Idempotent guard
            if case.visual_findings:
                logger.info("visual_findings_already_done", case_id=case_id)
                return

            # Mark as processing
            case.ai_status = AiStatus.PROCESSING
            await session.commit()

            personal_particulars = await _get_personal_particulars(session, case.patient_id)

            # ── Step 1: Clinical images ───────────────────────────── #
            clinical_bytes = await _get_images_by_type(session, case_id, ImageType.CLINICAL)
            clinical_result: dict = {}
            if clinical_bytes:
                prompt = DoctorImageAnalysisPrompts.clinical_description().format(
                    personal_particulars=personal_particulars
                )
                try:
                    raw = await asyncio.to_thread(call_llm, prompt, clinical_bytes)
                    clinical_result = await asyncio.to_thread(extract_json, raw)
                except Exception as exc:
                    logger.warning("visual_findings_clinical_failed", case_id=case_id, error=str(exc))

            # ── Step 2: Dermoscopy images (uses clinical as context) ─ #
            dermoscopy_bytes = await _get_images_by_type(session, case_id, ImageType.DERMOSCOPY)
            dermoscopy_result: dict = {}
            if dermoscopy_bytes:
                prompt = DoctorImageAnalysisPrompts.dermoscopic_description().format(
                    personal_particulars=personal_particulars,
                    clinical_description=json.dumps(clinical_result),
                )
                try:
                    raw = await asyncio.to_thread(call_llm, prompt, dermoscopy_bytes)
                    dermoscopy_result = await asyncio.to_thread(extract_json, raw)
                except Exception as exc:
                    logger.warning("visual_findings_dermoscopy_failed", case_id=case_id, error=str(exc))

            # ── Step 3: Pathology images (uses clinical + dermoscopy) ─ #
            pathology_bytes = await _get_images_by_type(session, case_id, ImageType.PATHOLOGY)
            pathology_result: dict = {}
            if pathology_bytes:
                prompt = DoctorImageAnalysisPrompts.pathological_description().format(
                    personal_particulars=personal_particulars,
                    clinical_description=json.dumps(clinical_result),
                    dermoscopic_description=json.dumps(dermoscopy_result),
                )
                try:
                    raw = await asyncio.to_thread(call_llm, prompt, pathology_bytes)
                    pathology_result = await asyncio.to_thread(extract_json, raw)
                except Exception as exc:
                    logger.warning("visual_findings_pathology_failed", case_id=case_id, error=str(exc))

            # ── Save ─────────────────────────────────────────────── #
            findings = {
                "clinical": clinical_result,
                "dermoscopy": dermoscopy_result,
                "pathology": pathology_result,
            }
            case.visual_findings = json.dumps(findings)
            case.ai_status = AiStatus.COMPLETED
            case.celery_task_id = None
            await session.commit()

            logger.info(
                "visual_findings_task_completed",
                case_id=case_id,
                has_clinical=bool(clinical_result),
                has_dermoscopy=bool(dermoscopy_result),
                has_pathology=bool(pathology_result),
            )

    try:
        _run_async(_run())
    except SoftTimeLimitExceeded:
        logger.error("visual_findings_task_timeout", case_id=case_id)

        async def _mark_failed():
            engine = _make_engine()
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await _fail_case(session, case_id, "Task timed out")

        _run_async(_mark_failed())
    except Exception as exc:
        logger.error("visual_findings_task_error", case_id=case_id, error=str(exc))

        async def _mark_error():
            engine = _make_engine()
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await _fail_case(session, case_id, str(exc))

        _run_async(_mark_error())
        raise
