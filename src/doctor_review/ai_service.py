"""
doctor_review/ai_service.py — Doctor Review AI-Assist Functions
================================================================

All functions here are STATELESS — they read case data from the DB,
call Gemini (or Whisper), and return a suggestion. Nothing is saved.
The doctor decides whether to accept, edit, or reject each suggestion.
The review is only saved when the doctor calls POST/PATCH /review.

FUNCTIONS
---------
get_technical_complaints  → translate patient language to clinical terms
generate_new_differential → fresh AI differential incorporating all context
get_first_question        → first clarifying question for the doctor
get_next_question         → next Q&A round (loop continues until no doubts)
generate_summary          → final clinical summary draft
generate_treatment_plan   → structured treatment plan + prescription
transcribe_voice_note     → Whisper audio → transcript string

CONTEXT BUILDING
----------------
All functions need the same case context:
  - visual_description (from VisualDescription table)
  - differential_diagnosis (from DifferentialDiagnosis table)
  - conversation_history (from Message table — patient Q&A)
  - patient age + sex (from PatientProfile)

_build_case_context() fetches all of this once and returns a dict.

GEMINI CALL PATTERN
-------------------
All AI calls use:
    answer = await asyncio.to_thread(call_gemini, prompt)
This runs the synchronous call_gemini() in a thread pool so it
doesn't block the async event loop.

WHISPER CALL PATTERN
--------------------
Whisper uses the OpenAI SDK:
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    result = await client.audio.transcriptions.create(...)
This is natively async — no asyncio.to_thread needed.
"""

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.ai.gemini_client import call_gemini
from src.ai.prompts.doctor_review_prompts import DoctorReviewPrompts
from src.exceptions import (
    AIServiceException,
    BadRequestException,
    CaseNotFoundException,
)
from src.logger import get_logger
from src.models.case import AiStatus, Case
from src.models.differential_diagnosis import DifferentialDiagnosis
from src.models.message import Message, MessageRole
from src.models.patient_profile import PatientProfile
from src.models.user import User
from src.models.visual_description import VisualDescription
from src.doctor_review.schemas import (
    AiComplaintsResponse,
    AiDiagnosisResponse,
    AiQuestionResponse,
    AiSummaryResponse,
    AiTreatmentPlanResponse,
    NextQuestionRequest,
    VoiceNoteResponse,
)

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Guard: only doctor assigned to this case can call AI-assist
# ------------------------------------------------------------------ #

async def _load_case_for_doctor_ai(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> Case:
    """Load case, verify doctor is assigned, and verify AI analysis is complete."""
    result = await db.execute(
        select(Case)
        .where(Case.id == case_id)
        .options(selectinload(Case.doctor_review))
    )
    case = result.scalar_one_or_none()

    if case is None or case.doctor_id != doctor.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before doctor AI-assist is available."
        )

    return case


# ------------------------------------------------------------------ #
# Context builder — shared by all AI-assist functions
# ------------------------------------------------------------------ #

async def _build_case_context(db: AsyncSession, case: Case) -> dict:
    """
    Fetch visual description, differential, conversation history,
    and patient demographics for a case.

    Returns a dict with keys:
        visual_description, differential_diagnosis,
        conversation_history, age, sex
    """
    # Visual description (latest round)
    vd_result = await db.execute(
        select(VisualDescription)
        .where(VisualDescription.case_id == case.id)
        .order_by(VisualDescription.round_number.desc())
        .limit(1)
    )
    vd = vd_result.scalar_one_or_none()
    visual_description = vd.description_json if vd else "{}"

    # Differential (final)
    dd_result = await db.execute(
        select(DifferentialDiagnosis)
        .where(
            DifferentialDiagnosis.case_id == case.id,
            DifferentialDiagnosis.is_final.is_(True),
        )
        .order_by(DifferentialDiagnosis.round_number.desc())
        .limit(1)
    )
    dd = dd_result.scalar_one_or_none()
    differential_diagnosis = dd.diagnosis_json if dd else "{}"

    # Patient Q&A conversation
    msgs_result = await db.execute(
        select(Message)
        .where(Message.case_id == case.id)
        .order_by(Message.round_number, Message.question_index)
    )
    messages = list(msgs_result.scalars().all())

    conv_lines: list[str] = []
    for m in messages:
        if m.role == MessageRole.AI:
            try:
                data = json.loads(m.content)
                if data.get("sentinel"):
                    continue
                q_text = data.get("question", "")
                if q_text:
                    conv_lines.append(f"Q: {q_text}")
            except (ValueError, TypeError):
                pass
        elif m.role == MessageRole.PATIENT:
            conv_lines.append(f"A: {m.content}")

    conversation_history = "\n".join(conv_lines) if conv_lines else "No Q&A on record."

    # Patient demographics
    age = "Unknown"
    sex = "Unknown"
    if case.is_for_self:
        profile_result = await db.execute(
            select(PatientProfile).where(PatientProfile.user_id == case.patient_id)
        )
        profile = profile_result.scalar_one_or_none()
        if profile:
            if profile.date_of_birth:
                from datetime import date
                today = date.today()
                dob = profile.date_of_birth
                age = str(today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day)))
            if profile.gender:
                sex = profile.gender
    else:
        # Dependent flow
        if case.dependent_dob:
            from datetime import date
            today = date.today()
            dob = case.dependent_dob
            age = str(today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day)))
        if case.dependent_gender:
            sex = case.dependent_gender

    return {
        "visual_description": visual_description,
        "differential_diagnosis": differential_diagnosis,
        "conversation_history": conversation_history,
        "age": age,
        "sex": sex,
    }


def _parse_json_safe(raw: str) -> str:
    """Return raw string — Gemini may return markdown-wrapped JSON, strip fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw
    return raw


# ------------------------------------------------------------------ #
# 1. Technical Complaints
# ------------------------------------------------------------------ #

async def get_technical_complaints(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> AiComplaintsResponse:
    """
    Translate patient-language complaint into technical dermatological terms.
    Returns a list of checkbox items for the doctor to confirm.
    """
    case = await _load_case_for_doctor_ai(db, doctor, case_id)
    ctx = await _build_case_context(db, case)

    prompt = DoctorReviewPrompts.get_technical_complaints().format(
        visual_description=ctx["visual_description"],
        differential_diagnosis=ctx["differential_diagnosis"],
        conversation_history=ctx["conversation_history"],
        age=ctx["age"],
        sex=ctx["sex"],
    )

    raw = await asyncio.to_thread(call_gemini, prompt)
    raw = _parse_json_safe(raw)

    complaints: list[str] = []
    try:
        data = json.loads(raw)
        complaints = data.get("Complaint", [])
    except (ValueError, TypeError):
        raise AIServiceException(message="AI returned an unexpected format for complaints.")

    logger.info("ai_complaints_generated", case_id=case_id)
    return AiComplaintsResponse(case_id=case_id, complaints=complaints)


# ------------------------------------------------------------------ #
# 2. Generate New Differential
# ------------------------------------------------------------------ #

async def generate_new_differential(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> AiDiagnosisResponse:
    """
    Generate a fresh doctor-facing differential incorporating all available context.
    """
    case = await _load_case_for_doctor_ai(db, doctor, case_id)
    ctx = await _build_case_context(db, case)

    # Prescription field — not stored separately; use presenting_complaint as context
    prescription = case.presenting_complaint or "Not available"

    prompt = DoctorReviewPrompts.generate_diagnosis().format(
        conversation_history=ctx["conversation_history"],
        visual_description=ctx["visual_description"],
        prescription=prescription,
        age=ctx["age"],
        sex=ctx["sex"],
    )

    raw = await asyncio.to_thread(call_gemini, prompt)
    diagnosis_json = _parse_json_safe(raw)

    logger.info("ai_diagnosis_generated", case_id=case_id)
    return AiDiagnosisResponse(case_id=case_id, diagnosis_json=diagnosis_json)


# ------------------------------------------------------------------ #
# 3. First Clarifying Question
# ------------------------------------------------------------------ #

async def get_first_question(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> AiQuestionResponse:
    """
    Run generate_doctor_doubts() → generate_doctor_questions() to produce
    the first clarifying question for the doctor.
    """
    case = await _load_case_for_doctor_ai(db, doctor, case_id)
    ctx = await _build_case_context(db, case)

    # Step 1: check if there's a doubt
    doubts_prompt = DoctorReviewPrompts.generate_doctor_doubts().format(
        conversation=ctx["conversation_history"],
        visual_description=ctx["visual_description"],
        diagnoses=ctx["differential_diagnosis"],
        age=ctx["age"],
        sex=ctx["sex"],
        questions_left=5,
    )

    doubts_raw = await asyncio.to_thread(call_gemini, doubts_prompt)
    doubts_raw = _parse_json_safe(doubts_raw)

    try:
        doubts_data = json.loads(doubts_raw)
    except (ValueError, TypeError):
        raise AIServiceException(message="AI returned unexpected format for doubts.")

    if doubts_data.get("doubt_present") == "no":
        return AiQuestionResponse(
            case_id=case_id,
            question="No further clarification needed.",
            answer_options=[],
            reason="AI has sufficient information to proceed.",
            has_more=False,
        )

    # Step 2: convert doubt → question
    doubts_str = json.dumps(doubts_data.get("doubt", []))
    question_prompt = DoctorReviewPrompts.generate_doctor_questions().format(
        conversation_history=ctx["conversation_history"],
        visual_description=ctx["visual_description"],
        diagnoses=ctx["differential_diagnosis"],
        doubts=doubts_str,
        age=ctx["age"],
        sex=ctx["sex"],
    )

    question_raw = await asyncio.to_thread(call_gemini, question_prompt)
    question_raw = _parse_json_safe(question_raw)

    try:
        q_data = json.loads(question_raw)
    except (ValueError, TypeError):
        raise AIServiceException(message="AI returned unexpected format for question.")

    logger.info("ai_first_question_generated", case_id=case_id)
    return AiQuestionResponse(
        case_id=case_id,
        question=q_data.get("question", ""),
        answer_options=q_data.get("answer_options", []),
        reason=q_data.get("reason", ""),
        has_more=True,
    )


# ------------------------------------------------------------------ #
# 4. Next Question (Q&A loop)
# ------------------------------------------------------------------ #

async def get_next_question(
    db: AsyncSession,
    doctor: User,
    case_id: str,
    request: NextQuestionRequest,
) -> AiQuestionResponse:
    """
    Given the full Q&A history so far, decide if another question is needed.
    Returns has_more=False when the AI is satisfied.
    """
    case = await _load_case_for_doctor_ai(db, doctor, case_id)
    ctx = await _build_case_context(db, case)

    # Build conversation including doctor Q&A history
    qa_lines = []
    for qa in request.qa_history:
        qa_lines.append(f"Doctor Q: {qa.question}")
        qa_lines.append(f"Doctor A: {qa.answer}")

    full_conversation = ctx["conversation_history"]
    if qa_lines:
        full_conversation += "\n\n--- Doctor Q&A ---\n" + "\n".join(qa_lines)

    doubts_prompt = DoctorReviewPrompts.generate_doctor_doubts().format(
        conversation=full_conversation,
        visual_description=ctx["visual_description"],
        diagnoses=ctx["differential_diagnosis"],
        age=ctx["age"],
        sex=ctx["sex"],
        questions_left=request.questions_left,
    )

    doubts_raw = await asyncio.to_thread(call_gemini, doubts_prompt)
    doubts_raw = _parse_json_safe(doubts_raw)

    try:
        doubts_data = json.loads(doubts_raw)
    except (ValueError, TypeError):
        raise AIServiceException(message="AI returned unexpected format for doubts.")

    if doubts_data.get("doubt_present") == "no" or request.questions_left <= 0:
        return AiQuestionResponse(
            case_id=case_id,
            question="No further clarification needed.",
            answer_options=[],
            reason="AI has sufficient information to proceed.",
            has_more=False,
        )

    doubts_str = json.dumps(doubts_data.get("doubt", []))
    question_prompt = DoctorReviewPrompts.generate_doctor_questions().format(
        conversation_history=full_conversation,
        visual_description=ctx["visual_description"],
        diagnoses=ctx["differential_diagnosis"],
        doubts=doubts_str,
        age=ctx["age"],
        sex=ctx["sex"],
    )

    question_raw = await asyncio.to_thread(call_gemini, question_prompt)
    question_raw = _parse_json_safe(question_raw)

    try:
        q_data = json.loads(question_raw)
    except (ValueError, TypeError):
        raise AIServiceException(message="AI returned unexpected format for question.")

    logger.info("ai_next_question_generated", case_id=case_id, questions_left=request.questions_left)
    return AiQuestionResponse(
        case_id=case_id,
        question=q_data.get("question", ""),
        answer_options=q_data.get("answer_options", []),
        reason=q_data.get("reason", ""),
        has_more=True,
    )


# ------------------------------------------------------------------ #
# 5. Final Clinical Summary
# ------------------------------------------------------------------ #

async def generate_summary(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> AiSummaryResponse:
    """
    Generate final clinical summary for the medical record.
    Uses confirmed_diagnosis from the doctor's review if available,
    otherwise falls back to the AI's most probable diagnosis.
    """
    case = await _load_case_for_doctor_ai(db, doctor, case_id)
    ctx = await _build_case_context(db, case)

    # Use doctor's confirmed diagnosis if available
    final_diagnosis = case.case_title or "Not yet confirmed"
    if case.doctor_review and case.doctor_review.confirmed_diagnosis:
        final_diagnosis = case.doctor_review.confirmed_diagnosis

    prompt = DoctorReviewPrompts.generate_final_summary().format(
        conversation=ctx["conversation_history"],
        visual_description=ctx["visual_description"],
        final_diagnosis=final_diagnosis,
        age=ctx["age"],
        sex=ctx["sex"],
    )

    raw = await asyncio.to_thread(call_gemini, prompt)
    summary_json = _parse_json_safe(raw)

    logger.info("ai_summary_generated", case_id=case_id)
    return AiSummaryResponse(case_id=case_id, summary_json=summary_json)


# ------------------------------------------------------------------ #
# 6. Treatment Plan
# ------------------------------------------------------------------ #

async def generate_treatment_plan(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> AiTreatmentPlanResponse:
    """
    Generate structured treatment plan + prescription for doctor sign-off.
    """
    case = await _load_case_for_doctor_ai(db, doctor, case_id)
    ctx = await _build_case_context(db, case)

    final_diagnosis = case.case_title or "Not yet confirmed"
    if case.doctor_review and case.doctor_review.confirmed_diagnosis:
        final_diagnosis = case.doctor_review.confirmed_diagnosis

    prompt = DoctorReviewPrompts.generate_treatment_plan().format(
        conversation=ctx["conversation_history"],
        final_diagnosis=final_diagnosis,
        age=ctx["age"],
        sex=ctx["sex"],
    )

    raw = await asyncio.to_thread(call_gemini, prompt)
    treatment_json = _parse_json_safe(raw)

    logger.info("ai_treatment_plan_generated", case_id=case_id)
    return AiTreatmentPlanResponse(case_id=case_id, treatment_plan_json=treatment_json)


# ------------------------------------------------------------------ #
# 7. Voice Note → Whisper Transcript
# ------------------------------------------------------------------ #

async def transcribe_voice_note(
    db: AsyncSession,
    doctor: User,
    case_id: str,
    audio_bytes: bytes,
    filename: str,
) -> VoiceNoteResponse:
    """
    Send recorded audio to OpenAI Whisper and return the transcript.

    Supports: m4a, mp3, wav, webm, ogg (any format Whisper accepts).
    The transcript is returned to Flutter — the doctor decides what to
    do with it (paste into Ask AI, save as a review note, etc.).
    """
    from openai import AsyncOpenAI
    from src.config import settings

    # Verify doctor has access to this case
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()

    if case is None or case.doctor_id != doctor.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if not settings.OPENAI_API_KEY:
        raise AIServiceException(message="Voice transcription is not configured.")

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    import io
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename  # Whisper uses the filename extension to detect format

    try:
        result_obj = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="en",
        )
        transcript = result_obj.text.strip()
    except Exception as exc:
        logger.error("whisper_transcription_failed", case_id=case_id, error=str(exc))
        raise AIServiceException(message=f"Voice transcription failed: {exc}")

    logger.info("voice_note_transcribed", case_id=case_id, chars=len(transcript))
    return VoiceNoteResponse(case_id=case_id, transcript=transcript)
