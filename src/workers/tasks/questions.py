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
from src.ai.llm_router import call_llm
from src.ai.prompts.image_analysis_prompts import ImageAnalysisPrompts
from src.ai.prompts.patient_consultation_prompts import PatientConsultationPrompts
from src.config import settings
from src.exceptions import AIProviderException, StorageException
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import AiStatus, Case
from src.models.case_image import CaseImage
from src.models.differential_diagnosis import DifferentialDiagnosis
from src.models.doctor_review import DoctorReview
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
        [m for m in messages if m.role == MessageRole.AI and m.content != '{"sentinel": true}'],
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


async def _get_latest_visual_desc_full(session, case_id: str) -> tuple[str, dict]:
    """
    Return (description_json_str, description_dict) for the most recent VisualDescription.
    Used when carrying forward the existing description without re-running Gemini.
    """
    result = await session.execute(
        select(VisualDescription)
        .where(VisualDescription.case_id == case_id)
        .order_by(VisualDescription.round_number.desc())
        .limit(1)
    )
    vd = result.scalar_one_or_none()
    if vd is None:
        return "{}", {}
    try:
        desc_dict = json.loads(vd.description_json)
        return vd.description_json, desc_dict
    except (json.JSONDecodeError, TypeError):
        return vd.description_json, {}


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


async def _get_follow_up_context(session, case: Case) -> str:
    """
    Build a follow-up context block to inject into AI prompts.
    Returns empty string for new complaints (no original_case_id).
    For follow-ups, returns previous diagnosis, confirmed diagnosis,
    treatment, symptom progression, and a trimmed previous case summary.
    """
    if not case.original_case_id:
        return ""

    orig_result = await session.execute(select(Case).where(Case.id == case.original_case_id))
    orig = orig_result.scalar_one_or_none()
    if orig is None:
        return ""

    parts = ["This is a follow-up consultation. Previous visit context:"]

    if orig.case_title:
        parts.append(f"- Previous AI diagnosis: {orig.case_title}")

    if case.symptom_progression:
        parts.append(f"- Symptom progression since last visit: {case.symptom_progression}")

    dr_result = await session.execute(
        select(DoctorReview).where(DoctorReview.case_id == case.original_case_id)
    )
    dr = dr_result.scalar_one_or_none()
    if dr:
        if dr.confirmed_diagnosis:
            try:
                import json as _json
                _cd_list = _json.loads(dr.confirmed_diagnosis)
                _cd_str = ", ".join(_cd_list) if _cd_list else dr.confirmed_diagnosis
            except (ValueError, TypeError):
                _cd_str = dr.confirmed_diagnosis
            parts.append(f"- Doctor's confirmed diagnosis: {_cd_str}")
        if dr.treatment_plan_json:
            import json as _json
            try:
                plan = _json.loads(dr.treatment_plan_json)
                meds = plan.get("treatment_plan", {}).get("medications", [])
                if meds:
                    med_names = [m.get("medication", "") for m in meds if isinstance(m, dict)]
                    med_str = ", ".join(m for m in med_names if m)
                    if med_str:
                        parts.append(f"- Previous treatment: {med_str}")
            except Exception:
                pass

    if orig.case_summary:
        trimmed = orig.case_summary[:400] + "..." if len(orig.case_summary) > 400 else orig.case_summary
        parts.append(f"- Previous case summary: {trimmed}")

    return "\n".join(parts)


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
            follow_up_context = await _get_follow_up_context(session, case)

            if round_number == 0:
                # max_question_rounds is already set by the patient's explicit depth
                # selection (POST /cases/{id}/assessment-depth) before questions start.
                # Do NOT call question_numbers() here — it would silently override
                # the patient's chosen Quick / Standard / Full depth.
                if case.has_visible_lesion:
                    # Image-based first questions
                    image_bytes = await _download_case_images(session, case_id)
                    prompt = ImageAnalysisPrompts.first_question().format(
                        follow_up_context=follow_up_context,
                    )
                    response_text = call_llm(prompt, images=image_bytes, json_mode=True)
                    questions_data = gemini_client.extract_json(response_text)
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
                    prompt = PatientConsultationPrompts.generate_questions_from_complaints().format(
                        age=age,
                        sex=sex,
                        complaints=complaint,
                        follow_up_context=follow_up_context,
                    )
                    response_text = call_llm(prompt, json_mode=True)
                    questions_data = gemini_client.extract_json(response_text)

            else:
                # Round 1+: single combined call — think medically + generate patient question
                conv_history = _build_conversation_history(messages)
                prev_questions = _build_previous_questions_text(messages)

                combined_prompt = PatientConsultationPrompts.generate_question_from_context().format(
                    conversation=conv_history,
                    visual_description=visual_desc,
                    diagnoses=differential,
                    previous_questions=prev_questions,
                    prescription="None",
                    datetime=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    follow_up_context=follow_up_context,
                )
                response_text = call_llm(combined_prompt, json_mode=True)
                questions_data = gemini_client.extract_json(response_text)

                # The prompt is now instructed to ALWAYS return a question, so
                # doubt_present=="no" should not occur. If it does (e.g. older
                # admin-overridden prompt), log and fall through — the round
                # counter in refine_analysis_task is the only stop signal, which
                # respects the user's chosen question count.
                if questions_data.get("doubt_present") == "no":
                    logger.warning(
                        "no_more_doubts_received_but_continuing",
                        case_id=case_id,
                        round=round_number,
                        max_rounds=case.max_question_rounds,
                        note="prompt should always return a question; check admin prompt override",
                    )

            # Delete any sentinel message written by trigger_questions to claim
            # the slot before this task ran. Real messages replace it below.
            sentinel_result = await session.execute(
                select(Message).where(
                    Message.case_id == case_id,
                    Message.round_number == round_number,
                    Message.content == '{"sentinel": true}',
                )
            )
            for sentinel_msg in sentinel_result.scalars().all():
                await session.delete(sentinel_msg)

            # Save questions as Message rows
            # Gemini sometimes returns "questions" (lower) instead of "Questions" (upper)
            questions_list = (
                questions_data.get("Questions")
                or questions_data.get("questions")
                or []
            )
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

            # Mirror doubts to legacy GCS folder structure (best-effort)
            if questions_list:
                try:
                    from src.storage.legacy_sync import (
                        get_legacy_prefix,
                        build_doubts_txt,
                        mirror_text,
                    )
                    # Use the initial diagnosis (case_title) — fixed at first analysis,
                    # never updated — so all rounds write to the same GCS folder.
                    diag_name = case.case_title
                    if diag_name:
                        legacy_prefix = await get_legacy_prefix(session, case, diag_name)
                        if legacy_prefix:
                            # Build doubts.txt from all AI messages so far (one section per round)
                            all_msgs = await _get_all_messages(session, case_id)
                            ai_by_round: dict[int, list[dict]] = {}
                            for msg in all_msgs:
                                if msg.role == MessageRole.AI and msg.content != '{"sentinel": true}':
                                    try:
                                        q_data = json.loads(msg.content)
                                        ai_by_round.setdefault(msg.round_number, []).append(q_data)
                                    except Exception:
                                        pass
                            rounds_data = []
                            for rn in sorted(ai_by_round.keys()):
                                qs = ai_by_round[rn]
                                doubts_dict = {
                                    "doubt_present": "yes",
                                    "doubt": [
                                        {
                                            f"doubt{i + 1}": q.get("question", ""),
                                            "reason": q.get("reason", "Follow-up assessment required"),
                                        }
                                        for i, q in enumerate(qs)
                                    ],
                                }
                                rounds_data.append((rn, doubts_dict))
                            mirror_text(legacy_prefix, "doubts.txt", build_doubts_txt(rounds_data))
                except Exception:
                    pass

            if not questions_list:
                logger.warning(
                    "questions_generated_empty",
                    case_id=case_id,
                    round=round_number,
                    raw_keys=list(questions_data.keys()),
                    hint="Gemini returned no Questions list — check prompt output format",
                )
            else:
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
    except (AIProviderException, StorageException) as exc:
        logger.error("generate_questions_task_error", case_id=case_id, error=str(exc))
        if self.request.retries >= self.max_retries:
            _run_async(_fail_task_on_timeout_q(case_id, "generate_questions"))
            raise Ignore()
        raise self.retry(exc=exc)
    except Exception:
        logger.exception("generate_questions_task_unexpected_error", case_id=case_id)
        raise


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
                    guard_follow_up = await _get_follow_up_context(session, case)
                    guard_particulars = await _get_patient_particulars(session, case.patient_id)
                    await _finalize_case(session, case_id, conv_history, visual_desc, diff_json, guard_follow_up, guard_particulars)
                    await session.commit()
                raise Ignore()

            messages = await _get_all_messages(session, case_id)
            conv_history = _build_conversation_history(messages)
            visual_desc = await _get_latest_visual_desc(session, case_id)
            differential = await _get_latest_differential(session, case_id)
            patient_particulars = await _get_patient_particulars(session, case.patient_id)
            follow_up_context = await _get_follow_up_context(session, case)

            # Step 1: Revised differential from conversation (works for both paths)
            diff_prompt = PatientConsultationPrompts.diagnosis_analysis_from_conversation().format(
                conversation_history=conv_history,
                previous_differential=differential,
                visual_description=visual_desc,
                prescription="None",
                follow_up_context=follow_up_context,
            )
            diff_text = call_llm(diff_prompt, json_mode=True)
            new_diff = gemini_client.extract_json(diff_text)
            new_diff_json = json.dumps(new_diff)

            # Step 2: Updated visual description — only re-run if a new image was uploaded
            # since the last analysis. The image pixels don't change mid-consultation,
            # so re-describing the same image every round adds no diagnostic value.
            # If the patient uploads a new photo between rounds, the check detects it
            # and re-analysis runs automatically.
            if case.has_visible_lesion:
                latest_vd_result = await session.execute(
                    select(VisualDescription)
                    .where(VisualDescription.case_id == case_id)
                    .order_by(VisualDescription.created_at.desc())
                    .limit(1)
                )
                latest_vd_row = latest_vd_result.scalar_one_or_none()

                if latest_vd_row is None:
                    has_new_image = True  # No prior description — must analyse
                else:
                    new_img_result = await session.execute(
                        select(CaseImage)
                        .where(
                            CaseImage.case_id == case_id,
                            CaseImage.created_at > latest_vd_row.created_at,
                        )
                        .limit(1)
                    )
                    has_new_image = new_img_result.scalar_one_or_none() is not None

                if has_new_image:
                    image_bytes = await _download_case_images(session, case_id)
                    desc_prompt = ImageAnalysisPrompts.get_description_with_context().format(
                        personal_particulars=patient_particulars,
                        previous_conversation=conv_history,
                    )
                    desc_text = call_llm(desc_prompt, images=image_bytes, json_mode=True)
                    new_desc = gemini_client.extract_json(desc_text)
                    new_desc_json = json.dumps(new_desc)
                    logger.info("image_reanalysis_ran", case_id=case_id, new_round=new_round)
                else:
                    new_desc_json, new_desc = await _get_latest_visual_desc_full(session, case_id)
                    logger.info("image_reanalysis_skipped", case_id=case_id, new_round=new_round, reason="no_new_image")
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

            # Snapshot BEFORE _finalize_case — which overwrites case.case_title with the
            # final diagnosis. All mirror code below must use this snapshot so every round
            # writes to the same legacy GCS folder (keyed on the initial diagnosis).
            _mirror_case_title = case.case_title

            if new_round >= max_rounds:
                # Max rounds reached — finalize
                await _finalize_case(session, case_id, conv_history, visual_desc, new_diff_json, follow_up_context, patient_particulars)
            # else: generate_questions_task will be enqueued after commit

            should_generate_more = new_round < max_rounds

            await session.commit()
            logger.info(
                "refine_analysis_ok",
                case_id=case_id,
                new_round=new_round,
                diagnosis=most_probable_name,
            )

            # Mirror question_answer.txt to legacy GCS (best-effort)
            try:
                from src.storage.legacy_sync import (
                    get_legacy_prefix,
                    build_question_answer_txt,
                    mirror_text,
                )
                if _mirror_case_title:
                    legacy_prefix = await get_legacy_prefix(session, case, _mirror_case_title)
                    if legacy_prefix:
                        all_msgs = await _get_all_messages(session, case_id)
                        ai_msgs_sorted = sorted(
                            [m for m in all_msgs if m.role == MessageRole.AI and m.content != '{"sentinel": true}'],
                            key=lambda m: (m.round_number, m.question_index or 0),
                        )
                        pat_answers = {
                            (m.round_number, m.question_index): m.content
                            for m in all_msgs if m.role == MessageRole.PATIENT
                        }
                        qa_pairs: list[tuple] = []
                        for msg in ai_msgs_sorted:
                            try:
                                q_data = json.loads(msg.content)
                                q_text = q_data.get("question", "")
                                options = q_data.get("answer_options", [])
                            except Exception:
                                q_text = msg.content
                                options = []
                            answer = pat_answers.get((msg.round_number, msg.question_index), "")
                            qa_pairs.append(("assistant", q_text))
                            qa_pairs.append(("answer_list", options))
                            qa_pairs.append(("User", answer))
                        mirror_text(
                            legacy_prefix,
                            "question_answer.txt",
                            build_question_answer_txt(qa_pairs),
                        )
            except Exception:
                pass

            # Re-mirror ALL case images after each Q&A round.
            # This catches images the user uploaded after analysis started (race
            # condition: save_results_task only sees images in DB at that moment).
            try:
                from sqlalchemy import func as _func
                from src.storage.legacy_sync import (
                    build_study_metadata,
                    get_legacy_prefix,
                    mirror_bytes,
                    mirror_json,
                )
                if _mirror_case_title:
                    _prefix = await get_legacy_prefix(session, case, _mirror_case_title)
                    if _prefix:
                        _imgs_result = await session.execute(
                            select(CaseImage)
                            .where(CaseImage.case_id == case_id)
                            .order_by(CaseImage.upload_order)
                        )
                        _imgs = list(_imgs_result.scalars().all())
                        for _idx, _img in enumerate(_imgs, start=1):
                            try:
                                _img_bytes = gcs.download_bytes(_img.gcs_path)
                                _ext = (_img.mime_type or "image/jpeg").split("/")[-1]
                                if _ext == "jpeg":
                                    _ext = "jpg"
                                mirror_bytes(
                                    _prefix,
                                    f"uploaded_image_{_idx}.{_ext}",
                                    _img_bytes,
                                    _img.mime_type or "image/jpeg",
                                )
                            except Exception:
                                pass
                        # Also refresh study_metadata.json with up-to-date counts.
                        _n_diffs = (await session.execute(
                            select(_func.count(DifferentialDiagnosis.id))
                            .where(DifferentialDiagnosis.case_id == case_id)
                        )).scalar() or 1
                        _llm = getattr(settings, "DEFAULT_LLM_MODEL", None) or settings.DEFAULT_LLM_PROVIDER
                        mirror_json(
                            _prefix,
                            "study_metadata.json",
                            build_study_metadata(
                                prefix=_prefix,
                                llm_provider=_llm,
                                total_questions=case.max_question_rounds or 0,
                                n_differentials=_n_diffs,
                                timestamp=(case.created_at or datetime.now(tz=timezone.utc)).isoformat(),
                            ),
                        )
            except Exception:
                pass

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
    except (AIProviderException, StorageException) as exc:
        logger.error("refine_analysis_task_error", case_id=case_id, error=str(exc))
        if self.request.retries >= self.max_retries:
            _run_async(_fail_task_on_timeout_q(case_id, "refine_analysis"))
            raise Ignore()
        raise self.retry(exc=exc)
    except Exception:
        logger.exception("refine_analysis_task_unexpected_error", case_id=case_id)
        raise


# ------------------------------------------------------------------ #
# Finalization helper
# ------------------------------------------------------------------ #

def _dict_to_numbered_summary(d: dict) -> str:
    """Convert a dict-typed case_summary (LLM returned wrong shape) to the standard numbered string."""
    diff = d.get("Differential Diagnosis", d.get("differential_diagnosis", {}))
    if isinstance(diff, dict):
        diff_str = ", ".join(f"{k} ({v})" for k, v in diff.items()) if diff else "Not available"
    elif isinstance(diff, list):
        _parts = []
        for item in diff:
            if isinstance(item, dict):
                _n = item.get("diagnosis", "")
                _l = item.get("likelihood", item.get("confidence", ""))
                _parts.append(f"{_n} ({_l})" if _l else _n)
            else:
                _parts.append(str(item))
        diff_str = ", ".join(_parts) if _parts else "Not available"
    else:
        diff_str = str(diff) if diff else "Not available"

    return (
        f"1. **Age**: {d.get('Age', 'Not provided')} "
        f"2. **Sex**: {d.get('Sex', 'Not provided')} "
        f"3. **Chief Complaint**: {d.get('Chief Complaint', 'Not provided')} "
        f"4. **History**: {d.get('History', 'Not provided')} "
        f"5. **Photograph Analysis**: {d.get('Photograph Analysis', 'Not provided')} "
        f"6. **Most Probable Diagnosis**: {d.get('Most Probable Diagnosis', 'Not determined')} "
        f"7. **Differential Diagnosis**: {diff_str}"
    )


async def _finalize_case(
    session,
    case_id: str,
    conv_history: str,
    visual_desc: str,
    differential_json: str,
    follow_up_context: str = "",
    patient_particulars: str = "Age: unknown, Sex: unknown",
) -> None:
    """
    Generate final case summary and mark consultation as complete.
    Called when max_question_rounds is reached or doctor doubts end.
    follow_up_context is passed through from the Q&A tasks so the summary
    references the previous visit when this is a follow-up case.
    patient_particulars provides age/sex so the summary is never "Not provided".
    """
    try:
        summary_prompt = PatientConsultationPrompts.make_case_summary().format(
            conversation_history=conv_history,
            visual_language_model_text=visual_desc,
            possible_diagnoses=differential_json,
            personal_particulars=patient_particulars,
            follow_up_context=follow_up_context,
        )
        summary_text = call_llm(summary_prompt, json_mode=True)
        summary_data = gemini_client.extract_json(summary_text)
        case_summary = summary_data.get("case_summary", "Consultation complete.")
        # LLM occasionally returns case_summary as a nested dict instead of a string —
        # convert to the standard numbered format so Flutter always gets one shape.
        if isinstance(case_summary, dict):
            case_summary = _dict_to_numbered_summary(case_summary)
    except (AIProviderException, Exception) as exc:
        logger.warning("make_case_summary_failed", case_id=case_id, error=str(exc))
        case_summary = "AI consultation rounds complete. Please review with your doctor."

    # Extract final most-probable diagnosis to update the case title.
    # This overwrites the initial title set by save_results_task so the
    # History tab reflects the Q&A-refined diagnosis, not the first-pass one.
    final_title: str | None = None
    try:
        diff = json.loads(differential_json)
        mpd = diff.get("most_probable_diagnosis", {})
        if isinstance(mpd, dict):
            final_title = mpd.get("diagnosis")
        elif isinstance(mpd, str):
            final_title = mpd
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    result = await session.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    _patient_id = None
    _case_number = None
    if case:
        case.case_summary = case_summary
        if final_title:
            case.case_title = final_title
        _patient_id = case.patient_id
        _case_number = case.case_number
    logger.info("case_finalized", case_id=case_id, final_title=final_title)

    if _patient_id:
        display_id = f"AI-{_case_number}" if _case_number else case_id[:8].upper()
        try:
            from src.workers.tasks.notifications import notify_patient_ai_complete
            notify_patient_ai_complete.delay(
                patient_id=_patient_id,
                case_id=case_id,
                display_id=display_id,
            )
        except Exception as exc:
            logger.warning("notify_patient_ai_complete_enqueue_failed", case_id=case_id, error=str(exc))


# ------------------------------------------------------------------ #
# Timeout helper
# ------------------------------------------------------------------ #

async def _fail_task_on_timeout_q(case_id: str, task_name: str) -> None:
    # Q&A tasks must NOT set ai_status=FAILED — that field represents the
    # initial image analysis, which already completed. Overwriting it would
    # block submit_answers and finish_conversation for the patient.
    # Just log the failure and leave the case in its current state so the
    # patient can still use "Finish Early" to exit the conversation.
    logger.error("qa_task_timed_out", case_id=case_id, task=task_name)
