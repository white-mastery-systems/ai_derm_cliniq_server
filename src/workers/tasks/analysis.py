"""
workers/tasks/analysis.py — AI Analysis Celery Task Chain
==========================================================

TASK CHAIN (ISS-001 guard — ONE sequential chain, never parallel)
-----------------------------------------------------------------
  inspect_images_task(case_id)
      ↓ returns case_id if images are adequate; aborts chain if not
  analyse_images_task(case_id)
      ↓ returns {"case_id": ..., "description_json": ..., "diagnosis_json": ...}
  save_results_task(result_dict)
      → saves VisualDescription + DifferentialDiagnosis rows
      → updates Case: ai_status=completed, case_summary

The chain is triggered as:
    chain(
        inspect_images_task.s(case_id),
        analyse_images_task.s(),
        save_results_task.s(),
    ).apply_async()

GATE PATTERN (ISS-003 guard — abort on quality failure)
--------------------------------------------------------
If inspect_images_task finds the images inadequate, it:
  1. Updates case.ai_status = FAILED and case.celery_task_id = None
  2. Raises celery.exceptions.Ignore — stops the chain without marking
     the task itself as failed (which would look like a worker crash)

TIMEOUTS (ISS-008 guard — prevent hanging AI calls)
----------------------------------------------------
Each task declares time_limit (hard kill) and soft_time_limit (SIGTERM).
The soft limit fires first → task can clean up DB state before dying.
On soft_time_limit, SoftTimeLimitExceeded is raised inside the task.

IDEMPOTENCY (ISS-009 guard — safe to call twice)
------------------------------------------------
The service layer checks ai_status == PENDING before enqueuing.
But tasks also double-check at the start to handle the race where
two requests arrive simultaneously before the status update commits.

DB ACCESS IN TASKS
------------------
Celery workers are synchronous processes. We use asyncio.run() to
execute our async SQLAlchemy queries inside each task. This creates
a fresh event loop per task invocation, which is correct — each task
is independent and long-lived connections in workers are problematic.
"""

import asyncio
import json

from celery import chain
from celery.exceptions import Ignore, SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.ai.llm_router import call_llm, extract_json
from src.ai.prompts.image_analysis_prompts import ImageAnalysisPrompts
from src.ai.prompts.patient_consultation_prompts import PatientConsultationPrompts
from src.config import settings
from src.exceptions import AIProviderException, StorageException
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import AiStatus, Case, RedFlagStatus
from src.models.case_image import CaseImage
from src.models.differential_diagnosis import DifferentialDiagnosis
from src.models.patient_profile import PatientProfile
from src.models.visual_description import VisualDescription
from src.storage import gcs
from src.workers.celery_app import celery_app

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# DB helper — sync wrapper for async SQLAlchemy
# ------------------------------------------------------------------ #

def _make_engine():
    """
    Create a fresh async engine for use inside a Celery task.

    WHY NullPool?
    -------------
    Celery workers are long-lived processes. Connection pools that
    persist across task invocations can cause issues (stale connections,
    fork-safety). NullPool creates a new connection for each use and
    closes it immediately — safe for forked workers.
    """
    return create_async_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,
    )


def _run_async(coro):
    """Run an async coroutine synchronously inside a Celery task."""
    return asyncio.run(coro)


async def _get_case(session: AsyncSession, case_id: str) -> Case | None:
    result = await session.execute(select(Case).where(Case.id == case_id))
    return result.scalar_one_or_none()


async def _get_case_images(session: AsyncSession, case_id: str) -> list[CaseImage]:
    result = await session.execute(
        select(CaseImage).where(CaseImage.case_id == case_id).order_by(CaseImage.upload_order)
    )
    return list(result.scalars().all())


async def _get_patient_particulars(session: AsyncSession, patient_id: str) -> str:
    """
    Build a personal particulars string for the AI prompt.
    Format: "Age: 34, Sex: Female" — or falls back to basic info.
    """
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
    """Mark the case as FAILED with a clear error recorded in case_summary."""
    case = await _get_case(session, case_id)
    if case:
        case.ai_status = AiStatus.FAILED
        case.celery_task_id = None
        case.case_summary = f"Analysis failed: {reason}"
        await session.commit()
    logger.warning("case_analysis_failed", case_id=case_id, reason=reason)


# ------------------------------------------------------------------ #
# Task 1 — Inspect Images
# ------------------------------------------------------------------ #

@celery_app.task(
    bind=True,
    name="src.workers.tasks.analysis.inspect_images_task",
    time_limit=120,
    soft_time_limit=100,
    max_retries=2,
    default_retry_delay=10,
)
def inspect_images_task(self, case_id: str) -> str:
    """
    Gate task: verify uploaded images are adequate for dermatological analysis.

    Returns case_id to pass down the chain.
    Raises Ignore (stopping the chain) if images are inadequate or missing.

    Steps:
    1. Fetch case + images from DB
    2. Idempotency check: abort if already processing or completed
    3. Download image bytes from GCS
    4. Call Gemini with inspect_images() prompt
    5. Parse response: {"answer":"yes"} or {"answer":"no","reason":"..."}
    6. On "no": mark case FAILED, raise Ignore
    7. On "yes": return case_id
    """
    logger.info("inspect_images_task_start", case_id=case_id)

    async def _run():
        engine = _make_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            case = await _get_case(session, case_id)
            if case is None:
                logger.error("inspect_images_task_case_not_found", case_id=case_id)
                raise Ignore()

            # Idempotency: only run if currently processing
            if case.ai_status not in (AiStatus.PROCESSING,):
                logger.warning(
                    "inspect_images_task_unexpected_status",
                    case_id=case_id,
                    status=case.ai_status,
                )
                raise Ignore()

            images = await _get_case_images(session, case_id)
            if not images:
                await _fail_case(session, case_id, "No images uploaded for this case")
                raise Ignore()

        # Download image bytes (outside session — no DB needed)
        image_bytes = []
        for img in images:
            try:
                data = gcs.download_bytes(img.gcs_path)
                image_bytes.append(data)
            except StorageException as exc:
                async with factory() as session:
                    await _fail_case(session, case_id, f"Could not download image: {exc}")
                raise Ignore() from exc

        # Call LLM inspect gate (with automatic fallback)
        try:
            prompt = ImageAnalysisPrompts.inspect_images()
            response_text = call_llm(prompt, images=image_bytes)
            result = extract_json(response_text)
        except AIProviderException as exc:
            async with factory() as session:
                await _fail_case(session, case_id, f"AI provider error: {exc}")
            raise Ignore() from exc

        if result.get("answer") != "yes":
            reason = result.get("reason", "Images are not adequate for analysis")
            async with factory() as session:
                await _fail_case(session, case_id, reason)
            raise Ignore()

        logger.info("inspect_images_task_ok", case_id=case_id)
        await engine.dispose()
        return case_id

    try:
        return _run_async(_run())
    except Ignore:
        raise
    except SoftTimeLimitExceeded:
        _run_async(_fail_task_on_timeout(case_id, "inspect_images"))
        raise Ignore()
    except Exception as exc:
        logger.error("inspect_images_task_error", case_id=case_id, error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------ #
# Task 2 — Analyse Images
# ------------------------------------------------------------------ #

@celery_app.task(
    bind=True,
    name="src.workers.tasks.analysis.analyse_images_task",
    time_limit=180,
    soft_time_limit=160,
    max_retries=1,
    default_retry_delay=15,
)
def analyse_images_task(self, case_id: str) -> dict:
    """
    Core analysis task: generate visual description + first differential.

    Receives case_id from inspect_images_task (via chain).
    Returns dict passed to save_results_task.

    Steps:
    1. Fetch case, images, patient profile from DB
    2. Download image bytes from GCS
    3. Call Gemini: get_description (visual description JSON)
    4. Call Gemini: generate_first_differential (diagnosis JSON)
    5. Return {"case_id": ..., "description_json": ..., "diagnosis_json": ...}
    """
    logger.info("analyse_images_task_start", case_id=case_id)

    async def _run():
        engine = _make_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as session:
            case = await _get_case(session, case_id)
            if case is None or case.ai_status != AiStatus.PROCESSING:
                raise Ignore()

            images = await _get_case_images(session, case_id)
            personal_particulars = await _get_patient_particulars(session, case.patient_id)

        # Download image bytes
        image_bytes = []
        for img in images:
            try:
                image_bytes.append(gcs.download_bytes(img.gcs_path))
            except StorageException as exc:
                async with factory() as session:
                    await _fail_case(session, case_id, f"Could not download image: {exc}")
                raise Ignore() from exc

        # Call LLM: visual description (with automatic fallback)
        try:
            desc_prompt = ImageAnalysisPrompts.get_description().format(
                personal_particulars=personal_particulars
            )
            desc_text = call_llm(desc_prompt, images=image_bytes)
            description_json = extract_json(desc_text)
        except AIProviderException as exc:
            async with factory() as session:
                await _fail_case(session, case_id, f"Description AI call failed: {exc}")
            raise Ignore() from exc

        # Call LLM: first differential (with automatic fallback)
        try:
            diag_prompt = ImageAnalysisPrompts.generate_first_differential().format(
                personal_particulars=personal_particulars
            )
            diag_text = call_llm(diag_prompt, images=image_bytes)
            diagnosis_json = extract_json(diag_text)
        except AIProviderException as exc:
            async with factory() as session:
                await _fail_case(session, case_id, f"Differential AI call failed: {exc}")
            raise Ignore() from exc

        logger.info("analyse_images_task_ok", case_id=case_id)
        await engine.dispose()
        return {
            "case_id": case_id,
            "description_json": json.dumps(description_json),
            "diagnosis_json": json.dumps(diagnosis_json),
        }

    try:
        return _run_async(_run())
    except Ignore:
        raise
    except SoftTimeLimitExceeded:
        _run_async(_fail_task_on_timeout(case_id, "analyse_images"))
        raise Ignore()
    except Exception as exc:
        logger.error("analyse_images_task_error", case_id=case_id, error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------ #
# Task 3 — Save Results
# ------------------------------------------------------------------ #

@celery_app.task(
    bind=True,
    name="src.workers.tasks.analysis.save_results_task",
    time_limit=60,
    soft_time_limit=50,
    max_retries=3,
    default_retry_delay=5,
)
def save_results_task(self, analysis_result: dict) -> None:
    """
    Persist AI results to DB and mark case as completed.

    Receives analysis_result dict from analyse_images_task (via chain):
    {
        "case_id": str,
        "description_json": str,   # JSON string
        "diagnosis_json": str,     # JSON string
    }

    Steps:
    1. Parse description_json → extract key fields for quick display
    2. Parse diagnosis_json → extract most_probable_diagnosis + confidence
    3. Insert VisualDescription row (round_number=0)
    4. Insert DifferentialDiagnosis row (round_number=0, is_final=True)
    5. Update Case: ai_status=COMPLETED, case_summary, celery_task_id=None
    """
    case_id = analysis_result.get("case_id", "unknown")
    logger.info("save_results_task_start", case_id=case_id)

    async def _run():
        engine = _make_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        description_json_str = analysis_result["description_json"]
        diagnosis_json_str = analysis_result["diagnosis_json"]

        # Parse for extracted fields
        try:
            desc = json.loads(description_json_str)
        except (json.JSONDecodeError, TypeError):
            desc = {}

        try:
            diag = json.loads(diagnosis_json_str)
        except (json.JSONDecodeError, TypeError):
            diag = {}

        most_probable = diag.get("most_probable_diagnosis", {})
        most_probable_name = (
            most_probable.get("diagnosis") if isinstance(most_probable, dict) else None
        )
        confidence = diag.get("confidence in answer") or diag.get("confidence")

        # Parse key_supporting_features into short symptom tag chips
        # e.g. "Dry, itchy patches; Redness; Chronic course" → ["Dry, itchy patches", "Redness", "Chronic course"]
        raw_features: str = (
            most_probable.get("key_supporting_features", "") if isinstance(most_probable, dict) else ""
        ) or ""
        symptom_tags: list[str] = []
        if raw_features:
            # Split on semicolon first, then comma if no semicolons found
            if ";" in raw_features:
                parts = [p.strip() for p in raw_features.split(";")]
            else:
                parts = [p.strip() for p in raw_features.split(",")]
            # Keep only non-empty tags under 60 chars
            symptom_tags = [p for p in parts if p and len(p) <= 60][:8]

        overall_description = desc.get("overall_description")
        type_of_lesion = desc.get("type_of_lesion")

        # Build a patient-readable summary
        case_summary_parts = []
        if type_of_lesion:
            case_summary_parts.append(f"Lesion type: {type_of_lesion}")
        if most_probable_name:
            case_summary_parts.append(f"Most probable diagnosis: {most_probable_name}")
        if confidence:
            case_summary_parts.append(f"Confidence: {confidence}")
        if overall_description:
            case_summary_parts.append(overall_description)
        case_summary = ". ".join(case_summary_parts) if case_summary_parts else "Analysis complete."

        async with factory() as session:
            case = await _get_case(session, case_id)
            if case is None:
                logger.error("save_results_task_case_not_found", case_id=case_id)
                return

            # VisualDescription row — only for image-based cases.
            # No-lesion path passes description_json="{}" (no image → no description).
            # Skipping the row means _get_latest_visual_desc returns the readable
            # "No visual description available." fallback instead of the raw "{}".
            if desc:  # desc is {} for no-lesion; non-empty dict for image path
                visual = VisualDescription(
                    id=new_uuid(),
                    case_id=case_id,
                    round_number=0,
                    description_json=description_json_str,
                    overall_description=overall_description,
                )
                session.add(visual)

            # DifferentialDiagnosis row
            differential = DifferentialDiagnosis(
                id=new_uuid(),
                case_id=case_id,
                round_number=0,
                is_final=True,
                diagnosis_json=diagnosis_json_str,
                most_probable_diagnosis=most_probable_name,
                confidence=str(confidence) if confidence else None,
            )
            session.add(differential)

            # Ask AI how many questions this case needs (recommendation for patient)
            recommended_rounds = 5  # safe default if call fails
            try:
                rounds_prompt = PatientConsultationPrompts.question_numbers().format(
                    diagnoses=diagnosis_json_str
                )
                rounds_text = call_llm(rounds_prompt)
                rounds_data = extract_json(rounds_text)
                raw = rounds_data.get("no_of_questions")
                if isinstance(raw, int) and 1 <= raw <= 15:
                    recommended_rounds = raw
            except Exception:
                pass  # non-critical — default is fine

            # Update case
            case.ai_status = AiStatus.COMPLETED
            case.celery_task_id = None
            case.case_summary = case_summary
            case.case_title = most_probable_name
            case.symptom_tags = json.dumps(symptom_tags) if symptom_tags else None
            case.max_question_rounds = recommended_rounds  # AI recommendation

            await session.commit()

        logger.info(
            "save_results_task_ok",
            case_id=case_id,
            diagnosis=most_probable_name,
            confidence=confidence,
        )
        await engine.dispose()

    try:
        _run_async(_run())
    except SoftTimeLimitExceeded:
        _run_async(_fail_task_on_timeout(case_id, "save_results"))
        raise Ignore()
    except Exception as exc:
        logger.error("save_results_task_error", case_id=case_id, error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------ #
# Red Flag Check Task
# ------------------------------------------------------------------ #

@celery_app.task(
    bind=True,
    name="src.workers.tasks.analysis.red_flag_check_task",
    time_limit=60,
    soft_time_limit=50,
    max_retries=1,
    default_retry_delay=10,
)
def red_flag_check_task(self, case_id: str, selected_symptoms: list[str] | None = None) -> None:
    """
    Systemic / red flag check — runs after all Q&A rounds complete.

    Checks the patient's complaint and Q&A answers for urgent symptoms
    (rapidly-changing mole, systemic fever, chest pain, etc.).

    Updates case:
      red_flag_status = CLEAR   — no urgent symptoms detected
      red_flag_status = FLAGGED — urgent symptoms found; saves flags + advice
    """
    from src.models.message import Message, MessageRole

    logger.info("red_flag_check_task_start", case_id=case_id)

    async def _run():
        engine = _make_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as session:
            case = await _get_case(session, case_id)
            if case is None or case.red_flag_status != RedFlagStatus.CHECKING:
                raise Ignore()

            # Gather complaint and all patient answers
            msgs_result = await session.execute(
                select(Message)
                .where(Message.case_id == case_id, Message.role == MessageRole.PATIENT)
                .order_by(Message.round_number, Message.question_index)
            )
            patient_answers = [m.content for m in msgs_result.scalars().all()]

            complaint = case.presenting_complaint or ""
            answers_text = "\n".join(f"- {a}" for a in patient_answers)

        # Build patient-reported symptoms string, stripping "None of the above"
        clean_symptoms = [
            s for s in (selected_symptoms or [])
            if s.strip().lower() != "none of the above"
        ]
        symptoms_text = "\n".join(f"- {s}" for s in clean_symptoms)

        try:
            prompt = PatientConsultationPrompts.red_flag_check(
                complaint=complaint,
                answers=answers_text,
                patient_reported_symptoms=symptoms_text,
            )
            raw = call_llm(prompt)
            result = extract_json(raw)
        except Exception as exc:
            logger.error("red_flag_check_llm_failed", case_id=case_id, error=str(exc))
            async with factory() as session:
                case = await _get_case(session, case_id)
                if case:
                    case.red_flag_status = RedFlagStatus.NOT_CHECKED
                    await session.commit()
            raise Ignore() from exc

        flags: list[str] = result.get("flags", [])
        advice: str | None = result.get("advice")
        has_flags = bool(flags)

        async with factory() as session:
            case = await _get_case(session, case_id)
            if case is None:
                return
            case.red_flag_status = RedFlagStatus.FLAGGED if has_flags else RedFlagStatus.CLEAR
            case.red_flags = json.dumps(flags)
            case.red_flag_advice = advice
            await session.commit()

        logger.info(
            "red_flag_check_complete",
            case_id=case_id,
            flagged=has_flags,
            flags=flags,
        )
        await engine.dispose()

    try:
        _run_async(_run())
    except SoftTimeLimitExceeded:
        logger.error("red_flag_check_timeout", case_id=case_id)
        raise Ignore()
    except Exception as exc:
        logger.error("red_flag_check_task_error", case_id=case_id, error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------ #
# Timeout helper
# ------------------------------------------------------------------ #

async def _fail_task_on_timeout(case_id: str, task_name: str) -> None:
    """Called from soft_time_limit handler to mark case as failed."""
    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await _fail_case(session, case_id, f"{task_name} timed out")
    await engine.dispose()


# ------------------------------------------------------------------ #
# Task — Analyse Complaint (no visible lesion path)
# ------------------------------------------------------------------ #

@celery_app.task(
    bind=True,
    name="src.workers.tasks.analysis.analyse_complaint_task",
    time_limit=180,
    soft_time_limit=160,
    max_retries=1,
    default_retry_delay=15,
)
def analyse_complaint_task(self, case_id: str) -> dict:
    """
    Complaint-only analysis for cases where has_visible_lesion=False.

    No images are downloaded. The differential is generated purely from
    the patient's presenting_complaint text using generate_differential_from_complaints().

    Returns the same dict shape as analyse_images_task so that save_results_task
    can handle both paths identically:
      {"case_id": ..., "description_json": "{}", "diagnosis_json": ...}
    """
    logger.info("analyse_complaint_task_start", case_id=case_id)

    async def _run():
        engine = _make_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as session:
            case = await _get_case(session, case_id)
            if case is None or case.ai_status != AiStatus.PROCESSING:
                raise Ignore()

            personal_particulars = await _get_patient_particulars(session, case.patient_id)
            complaint = case.presenting_complaint or "No specific complaint provided."

        # Parse age / sex out of personal_particulars for prompt template vars
        age, sex = "unknown", "unknown"
        for part in personal_particulars.split(","):
            part = part.strip()
            if part.startswith("Age:"):
                age = part.split(":", 1)[1].strip()
            elif part.startswith("Sex:"):
                sex = part.split(":", 1)[1].strip()

        try:
            diag_prompt = PatientConsultationPrompts.generate_differential_from_complaints().format(
                age=age,
                sex=sex,
                complaints=complaint,
                prescription="None",
            )
            diag_text = call_llm(diag_prompt)
            diagnosis_json = extract_json(diag_text)
        except AIProviderException as exc:
            async with factory() as session:
                await _fail_case(session, case_id, f"Complaint differential AI call failed: {exc}")
            raise Ignore() from exc

        logger.info("analyse_complaint_task_ok", case_id=case_id)
        await engine.dispose()
        return {
            "case_id": case_id,
            "description_json": "{}",   # No visual description for no-lesion cases
            "diagnosis_json": json.dumps(diagnosis_json),
        }

    try:
        return _run_async(_run())
    except Ignore:
        raise
    except SoftTimeLimitExceeded:
        _run_async(_fail_task_on_timeout(case_id, "analyse_complaint"))
        raise Ignore()
    except Exception as exc:
        logger.error("analyse_complaint_task_error", case_id=case_id, error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------ #
# Chain factory — called from service layer
# ------------------------------------------------------------------ #

def build_analysis_chain(case_id: str, has_visible_lesion: bool = True):
    """
    Build the Celery task chain for one case.

    Returns a Celery Signature (not yet applied).
    The caller does .apply_async() to actually enqueue.

    Visible lesion chain (has_visible_lesion=True):
        inspect_images_task(case_id)
        → analyse_images_task(case_id)
        → save_results_task(result_dict)

    No visible lesion chain (has_visible_lesion=False):
        analyse_complaint_task(case_id)
        → save_results_task(result_dict)
    """
    if has_visible_lesion:
        return chain(
            inspect_images_task.s(case_id),
            analyse_images_task.s(),
            save_results_task.s(),
        )
    return chain(
        analyse_complaint_task.s(case_id),
        save_results_task.s(),
    )
