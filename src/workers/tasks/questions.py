"""
workers/tasks/questions.py — Q&A Conversation Celery Tasks
============================================================

TWO TASKS
---------
1. generate_questions_task(case_id)
   → Generates 3 follow-up questions for the current round.
   → Round 0: uses ImageAnalysisPrompts.first_question() with images
   → Round 1+: uses generate_doctor_doubts_patient() → generate_follow_up_questions()
   → Saves questions as Message(role=AI) rows
   → Also sets case.max_question_rounds (round 0 only) via question_numbers()

2. refine_analysis_task(case_id)
   → Runs after patient submits answers.
   → Builds full conversation history string from Message rows.
   → Calls diagnosis_analysis_from_conversation() → new DifferentialDiagnosis row
   → Calls get_description_with_context() → new VisualDescription row
   → Increments case.question_round.
   → If rounds remaining → enqueues generate_questions_task.
   → If max rounds reached → runs make_case_summary() → updates case_summary.

SEQUENTIAL FLOW (ISS-001 guard)
-------------------------------
One task per case at a time. generate_questions_task and refine_analysis_task
are never enqueued in parallel for the same case.

HISTORY FORMAT
--------------
Conversation history passed to AI prompts follows this format:

    Q1: How long have you had this rash?
    A1: 2–4 weeks
    Q2: Does it itch?
    A2: Yes, severely

AI message content is stored as JSON: {"question": "...", "answer_options": [...]}
Patient message content is stored as plain text (the selected option).

PRESCRIPTION
------------
Prescription OCR is Layer 7+. All prompts that accept {prescription}
receive "None" until that layer is built.
"""

import asyncio
import json
from datetime import datetime, timezone

from celery.exceptions import Ignore, SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from src.ai import gemini_client
from src.ai.prompts.image_analysis_prompts import ImageAnalysisPrompts
from src.ai.prompts.patient_consultation_prompts import PatientConsultationPrompts
from src.config import settings
from src.exceptions import AIProviderException, StorageException
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import AiStatus, Case
from src.models.case_image import CaseImage
from src.models.differential_diagnosis import DifferentialDiagnosis
from src.models.message import Message, MessageRole
from src.models.patient_profile import PatientProfile
from src.models.visual_description import VisualDescription
from src.storage import gcs
from src.workers.celery_app import celery_app
from src.workers.tasks.analysis import _make_engine, _run_async, _fail_case

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Conversation helpers
# ------------------------------------------------------------------ #

def _build_conversation_history(messages: list[Message]) -> str:
    """
    Build the human-readable conversation history string for AI prompts.

    Format:
        Q1: <question text>
        A1: <patient answer>
        Q2: <question text>
        A2: <patient answer>
        ...
    """
    ai_messages = sorted(
        [m for m in messages if m.role == MessageRole.AI],
        key=lambda m: (m.round_number, m.question_index or 0),
    )
    patient_messages = {
        (m.round_number, m.question_index): m.content
        for m in messages
        if m.role == MessageRole.PATIENT
    }

    lines = []
    for i, msg in enumerate(ai_messages, start=1):
        try:
            q_data = json.loads(msg.content)
            q_text = q_data.get("question", msg.content)
        except (json.JSONDecodeError, TypeError):
            q_text = msg.content

        answer = patient_messages.get((msg.round_number, msg.question_index), "")
        lines.append(f"Q{i}: {q_text}")
        if answer:
            lines.append(f"A{i}: {answer}")

    return "\n".join(lines)


def _build_previous_questions_text(messages: list[Message]) -> str:
    """Flat list of all previous questions (for deduplication prompt)."""
    lines = []
    for msg in messages:
        if msg.role == MessageRole.AI:
            try:
                q_data = json.loads(msg.content)
                lines.append(q_data.get("question", ""))
            except (json.JSONDecodeError, TypeError):
                lines.append(msg.content)
    return "\n".join(lines)


async def _get_patient_particulars(session, patient_id: str) -> str:
    result = await session.execute(
        select(PatientProfile).where(PatientProfile.user_id == patient_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        return "Age: unknown, Sex: unknown"
    parts = []
    if profile.date_of_birth:
        from datetime import date
        age = (date.today() - profile.date_of_birth).days // 365
        parts.append(f"Age: {age}")
    if profile.gender:
        parts.append(f"Sex: {profile.gender}")
    return ", ".join(parts) if parts else "Age: unknown, Sex: unknown"


async def _get_latest_visual_desc(session, case_id: str) -> str:
    result = await session.execute(
        select(VisualDescription)
        .where(VisualDescription.case_id == case_id)
        .order_by(VisualDescription.round_number.desc())
        .limit(1)
    )
    vd = result.scalar_one_or_none()
    if vd is None:
        return "No visual description available."
    try:
        data = json.loads(vd.description_json)
        return data.get("overall_description") or vd.overall_description or vd.description_json
    except (json.JSONDecodeError, TypeError):
        return vd.overall_description or "No description available."


async def _get_latest_differential(session, case_id: str) -> str:
    result = await session.execute(
        select(DifferentialDiagnosis)
        .where(DifferentialDiagnosis.case_id == case_id)
        .order_by(DifferentialDiagnosis.round_number.desc())
        .limit(1)
    )
    dd = result.scalar_one_or_none()
    return dd.diagnosis_json if dd else "{}"


async def _get_all_messages(session, case_id: str) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.case_id == case_id)
        .order_by(Message.round_number, Message.question_index, Message.created_at)
    )
    return list(result.scalars().all())


async def _download_case_images(session, case_id: str) -> list[bytes]:
    result = await session.execute(
        select(CaseImage)
        .where(CaseImage.case_id == case_id)
        .order_by(CaseImage.upload_order)
    )
    images = list(result.scalars().all())
    image_bytes = []
    for img in images:
        data = gcs.download_bytes(img.gcs_path)
        image_bytes.append(data)
    return image_bytes


# ------------------------------------------------------------------ #
# Task 1 — Generate Questions
# ------------------------------------------------------------------ #

@celery_app.task(
    bind=True,
    name="src.workers.tasks.questions.generate_questions_task",
    time_limit=120,
    soft_time_limit=100,
    max_retries=2,
    default_retry_delay=10,
)
def generate_questions_task(self, case_id: str) -> None:
    """
    Generate 3 follow-up questions for the current Q&A round and save them to DB.

    Round 0: ImageAnalysisPrompts.first_question() — image-based initial questions.
             Also calls question_numbers() to set max_question_rounds on the case.

    Round 1+: generate_doctor_doubts_patient() → generate_follow_up_questions()
              Pure text prompts — no image re-download needed.

    If doctor_doubts returns no doubt (doubt_present=no), finalize instead.
    """
    logger.info("generate_questions_task_start", case_id=case_id)

    async def _run():
        engine = _make_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as session:
            result = await session.execute(select(Case).where(Case.id == case_id))
            case = result.scalar_one_or_none()
            if case is None:
                raise Ignore()

            round_number = case.question_round
            messages = await _get_all_messages(session, case_id)
            visual_desc = await _get_latest_visual_desc(session, case_id)
            differential = await _get_latest_differential(session, case_id)
            patient_particulars = await _get_patient_particulars(session, case.patient_id)

            if round_number == 0:
                # max_question_rounds is already set by the patient's explicit depth
                # selection (POST /cases/{id}/assessment-depth) before questions start.
                # Do NOT call question_numbers() here — it would silently override
                # the patient's chosen Quick / Standard / Full depth.
                if case.has_visible_lesion:
                    # Image-based first questions
                    image_bytes = await _download_case_images(session, case_id)
                    try:
                        prompt = ImageAnalysisPrompts.first_question()
                        response_text = gemini_client.call_gemini(prompt, images=image_bytes)
                        questions_data = gemini_client.extract_json(response_text)
                    except AIProviderException as exc:
                        await _fail_case(session, case_id, f"Question generation failed: {exc}")
                        raise Ignore() from exc
                else:
                    # Complaint-only first questions (no visible lesion path)
                    age, sex = "unknown", "unknown"
                    for part in patient_particulars.split(","):
                        part = part.strip()
                        if part.startswith("Age:"):
                            age = part.split(":", 1)[1].strip()
                        elif part.startswith("Sex:"):
                            sex = part.split(":", 1)[1].strip()
                    complaint = case.presenting_complaint or ""
                    try:
                        prompt = PatientConsultationPrompts.generate_questions_from_complaints().format(
                            age=age,
                            sex=sex,
                            complaints=complaint,
                        )
                        response_text = gemini_client.call_gemini(prompt)
                        questions_data = gemini_client.extract_json(response_text)
                    except AIProviderException as exc:
                        await _fail_case(session, case_id, f"Question generation failed: {exc}")
                        raise Ignore() from exc

            else:
                # Round 1+: text-only doubts → questions pipeline
                conv_history = _build_conversation_history(messages)
                prev_questions = _build_previous_questions_text(messages)

                # Step 1: doctor doubts
                try:
                    doubts_prompt = PatientConsultationPrompts.generate_doctor_doubts_patient().format(
                        conversation=conv_history,
                        visual_description=visual_desc,
                        diagnoses=differential,
                        prescription="None",
                        datetime=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    )
                    doubts_text = gemini_client.call_gemini(doubts_prompt)
                    doubts_data = gemini_client.extract_json(doubts_text)
                except AIProviderException as exc:
                    await _fail_case(session, case_id, f"Doctor doubts generation failed: {exc}")
                    raise Ignore() from exc

                # If no doubts — finalize (no more questions needed)
                if doubts_data.get("doubt_present") != "yes":
                    logger.info("no_more_doubts_finalizing", case_id=case_id)
                    await _finalize_case(session, case_id, conv_history, visual_desc, differential)
                    await session.commit()
                    await engine.dispose()
                    return

                # Step 2: convert doubts to patient questions
                doubts_list = doubts_data.get("doubt", [])
                try:
                    questions_prompt = PatientConsultationPrompts.generate_follow_up_questions().format(
                        doubts=json.dumps(doubts_list),
                        conversation_history=conv_history,
                        diagnoses=differential,
                        visual_description=visual_desc,
                        previous_questions=prev_questions,
                        prescription="None",
                    )
                    response_text = gemini_client.call_gemini(questions_prompt)
                    questions_data = gemini_client.extract_json(response_text)
                except AIProviderException as exc:
                    await _fail_case(session, case_id, f"Follow-up question generation failed: {exc}")
                    raise Ignore() from exc

            # Save questions as Message rows
            questions_list = questions_data.get("Questions", [])
            for i, q in enumerate(questions_list):
                msg = Message(
                    id=new_uuid(),
                    case_id=case_id,
                    role=MessageRole.AI,
                    content=json.dumps(q),
                    round_number=round_number,
                    question_index=i,
                )
                session.add(msg)

            await session.commit()
            logger.info(
                "questions_generated",
                case_id=case_id,
                round=round_number,
                count=len(questions_list),
            )
        await engine.dispose()

    try:
        _run_async(_run())
    except Ignore:
        raise
    except SoftTimeLimitExceeded:
        _run_async(_fail_task_on_timeout_q(case_id, "generate_questions"))
        raise Ignore()
    except Exception as exc:
        logger.error("generate_questions_task_error", case_id=case_id, error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------ #
# Task 2 — Refine Analysis
# ------------------------------------------------------------------ #

@celery_app.task(
    bind=True,
    name="src.workers.tasks.questions.refine_analysis_task",
    time_limit=180,
    soft_time_limit=160,
    max_retries=1,
    default_retry_delay=15,
)
def refine_analysis_task(self, case_id: str) -> None:
    """
    Re-run analysis after patient submits answers for the current round.

    Steps:
    1. Build full conversation history from all Message rows.
    2. Call diagnosis_analysis_from_conversation() → revised differential.
    3. Call get_description_with_context() → updated visual description.
    4. Save new VisualDescription + DifferentialDiagnosis rows for new_round.
    5. Mark previous final differential as is_final=False.
    6. Increment case.question_round.
    7a. If rounds remaining → enqueue generate_questions_task.
    7b. If max rounds reached → finalize (make_case_summary).
    """
    logger.info("refine_analysis_task_start", case_id=case_id)

    async def _run():
        engine = _make_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as session:
            result = await session.execute(select(Case).where(Case.id == case_id))
            case = result.scalar_one_or_none()
            if case is None:
                raise Ignore()

            current_round = case.question_round
            new_round = current_round + 1

            # Guard: if chat/finish was called while this task was in the queue,
            # question_round was already set to max_question_rounds. Don't overwrite it —
            # just run finalization if case_summary is missing, then exit.
            if current_round >= case.max_question_rounds:
                if not case.case_summary:
                    messages = await _get_all_messages(session, case_id)
                    conv_history = _build_conversation_history(messages)
                    visual_desc = await _get_latest_visual_desc(session, case_id)
                    differential = await _get_latest_differential(session, case_id)
                    diff_json = differential.diagnosis_json if differential else "{}"
                    await _finalize_case(session, case_id, conv_history, visual_desc, diff_json)
                    await session.commit()
                raise Ignore()

            messages = await _get_all_messages(session, case_id)
            conv_history = _build_conversation_history(messages)
            visual_desc = await _get_latest_visual_desc(session, case_id)
            differential = await _get_latest_differential(session, case_id)
            patient_particulars = await _get_patient_particulars(session, case.patient_id)

            # Step 1: Revised differential from conversation (works for both paths)
            try:
                diff_prompt = PatientConsultationPrompts.diagnosis_analysis_from_conversation().format(
                    conversation_history=conv_history,
                    previous_differential=differential,
                    visual_description=visual_desc,
                    prescription="None",
                )
                diff_text = gemini_client.call_gemini(diff_prompt)
                new_diff = gemini_client.extract_json(diff_text)
                new_diff_json = json.dumps(new_diff)
            except AIProviderException as exc:
                await _fail_case(session, case_id, f"Analysis refinement failed: {exc}")
                raise Ignore() from exc

            # Step 2: Updated visual description — image path only
            if case.has_visible_lesion:
                image_bytes = await _download_case_images(session, case_id)
                try:
                    desc_prompt = ImageAnalysisPrompts.get_description_with_context().format(
                        personal_particulars=patient_particulars,
                        previous_conversation=conv_history,
                    )
                    desc_text = gemini_client.call_gemini(desc_prompt, images=image_bytes)
                    new_desc = gemini_client.extract_json(desc_text)
                    new_desc_json = json.dumps(new_desc)
                except AIProviderException as exc:
                    await _fail_case(session, case_id, f"Visual description update failed: {exc}")
                    raise Ignore() from exc
            else:
                # No visible lesion — no image to describe; carry forward empty description
                new_desc = {}
                new_desc_json = "{}"

            # Mark old final differential as non-final
            old_dd_result = await session.execute(
                select(DifferentialDiagnosis)
                .where(
                    DifferentialDiagnosis.case_id == case_id,
                    DifferentialDiagnosis.is_final.is_(True),
                )
            )
            for old_dd in old_dd_result.scalars().all():
                old_dd.is_final = False

            # Save new VisualDescription
            most_probable = new_diff.get("most_probable_diagnosis", {})
            most_probable_name = (
                most_probable.get("diagnosis") if isinstance(most_probable, dict) else None
            )
            confidence = new_diff.get("confidence in answer") or new_diff.get("confidence")

            new_vd = VisualDescription(
                id=new_uuid(),
                case_id=case_id,
                round_number=new_round,
                description_json=new_desc_json,
                overall_description=new_desc.get("overall_description"),
            )
            session.add(new_vd)

            # Save new DifferentialDiagnosis
            new_dd = DifferentialDiagnosis(
                id=new_uuid(),
                case_id=case_id,
                round_number=new_round,
                is_final=True,
                diagnosis_json=new_diff_json,
                most_probable_diagnosis=most_probable_name,
                confidence=str(confidence) if confidence else None,
            )
            session.add(new_dd)

            # Increment round
            case.question_round = new_round
            max_rounds = case.max_question_rounds

            if new_round >= max_rounds:
                # Max rounds reached — finalize
                await _finalize_case(session, case_id, conv_history, visual_desc, new_diff_json)
            # else: generate_questions_task will be enqueued after commit

            should_generate_more = new_round < max_rounds

            await session.commit()
            logger.info(
                "refine_analysis_ok",
                case_id=case_id,
                new_round=new_round,
                diagnosis=most_probable_name,
            )

        if should_generate_more:
            generate_questions_task.delay(case_id)

        await engine.dispose()

    try:
        _run_async(_run())
    except Ignore:
        raise
    except SoftTimeLimitExceeded:
        _run_async(_fail_task_on_timeout_q(case_id, "refine_analysis"))
        raise Ignore()
    except Exception as exc:
        logger.error("refine_analysis_task_error", case_id=case_id, error=str(exc))
        raise self.retry(exc=exc)


# ------------------------------------------------------------------ #
# Finalization helper
# ------------------------------------------------------------------ #

async def _finalize_case(
    session,
    case_id: str,
    conv_history: str,
    visual_desc: str,
    differential_json: str,
) -> None:
    """
    Generate final case summary and mark consultation as complete.
    Called when max_question_rounds is reached or doctor doubts end.
    """
    try:
        summary_prompt = PatientConsultationPrompts.make_case_summary().format(
            conversation_history=conv_history,
            visual_language_model_text=visual_desc,
            possible_diagnoses=differential_json,
        )
        summary_text = gemini_client.call_gemini(summary_prompt)
        summary_data = gemini_client.extract_json(summary_text)
        case_summary = summary_data.get("case_summary", "Consultation complete.")
    except (AIProviderException, Exception) as exc:
        logger.warning("make_case_summary_failed", case_id=case_id, error=str(exc))
        case_summary = "AI consultation rounds complete. Please review with your doctor."

    result = await session.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case:
        case.case_summary = case_summary
    logger.info("case_finalized", case_id=case_id)


# ------------------------------------------------------------------ #
# Timeout helper
# ------------------------------------------------------------------ #

async def _fail_task_on_timeout_q(case_id: str, task_name: str) -> None:
    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await _fail_case(session, case_id, f"{task_name} timed out")
    await engine.dispose()
