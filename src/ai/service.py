"""
ai/service.py — AI Analysis Business Logic
============================================

Three operations:
1. trigger_analysis  — validate + enqueue Celery chain (POST /analyze)
2. get_status        — return ai_status + task state (GET /status)
3. get_results       — return latest VisualDescription + Differential (GET /results)

IDEMPOTENCY GATE (ISS-009 guard)
---------------------------------
trigger_analysis checks ai_status before enqueuing. Only PENDING cases
can be triggered. If already PROCESSING or COMPLETED → 409 Conflict.
This prevents double-enqueuing if the Flutter app sends the request twice.

CELERY AVAILABILITY
-------------------
If Redis is not running, the chain.apply_async() call raises a connection
error. We catch it and return a 503 ServiceUnavailable so the client
knows to retry later rather than seeing a 500 crash.

ACCESS CONTROL
--------------
Only the patient who owns the case can trigger analysis.
Both patient and assigned doctor can read status and results.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai.schemas import (
    AiChatHistoryResponse,
    AiChatResponse,
    AnalysisAcceptedResponse,
    AnalysisResultsResponse,
    AnalysisStatusResponse,
    ChatMessageOut,
    DifferentialDiagnosisOut,
    VisualDescriptionOut,
)
from src.exceptions import (
    AIServiceException,
    BadRequestException,
    CaseNotFoundException,
    ConflictException,
    ForbiddenException,
)
from src.workers.tasks.analysis import build_analysis_chain
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import AiStatus, Case
from src.models.case_image import CaseImage
from src.models.differential_diagnosis import DifferentialDiagnosis
from src.models.user import User, UserRole
from src.models.visual_description import VisualDescription

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

async def _get_case_with_access(db: AsyncSession, case_id: str, user: User) -> Case:
    """Load case and verify user has access. Returns CaseNotFoundException for both
    'not found' and 'not accessible' to prevent case enumeration.
    ADMIN always passes — they can read any case."""
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    if user.role == UserRole.ADMIN:
        return case
    if user.role == UserRole.PATIENT and case.patient_id != user.id:
        raise CaseNotFoundException()
    if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
        raise CaseNotFoundException()
    return case


async def _count_images(db: AsyncSession, case_id: str) -> int:
    result = await db.execute(
        select(CaseImage).where(CaseImage.case_id == case_id)
    )
    return len(result.scalars().all())


# ------------------------------------------------------------------ #
# Trigger Analysis
# ------------------------------------------------------------------ #

async def trigger_analysis(
    db: AsyncSession,
    patient: User,
    case_id: str,
) -> AnalysisAcceptedResponse:
    """
    Validate the case and enqueue the Celery analysis chain.

    Guards:
    1. Case exists + patient owns it
    2. Consent given
    3. At least one image uploaded
    4. ai_status == PENDING (idempotency — no double-enqueue)

    On success:
    - Sets case.ai_status = PROCESSING
    - Enqueues inspect → analyse → save chain
    - Stores celery_task_id in case

    Returns 202 Accepted with task_id.
    """
    # Only patients can trigger analysis
    if patient.role != UserRole.PATIENT:
        raise ForbiddenException(message="Only the patient can trigger AI analysis")

    case = await _get_case_with_access(db, case_id, patient)

    # Consent gate
    if not case.consent_ai_analysis:
        raise ForbiddenException(
            message="Patient must give consent before triggering AI analysis"
        )

    # No-lesion flow: presenting_complaint is the only clinical input — require it before analysis
    if not case.has_visible_lesion and not case.presenting_complaint:
        raise BadRequestException(
            message="Please describe your sensations or symptoms "
                    "(presenting_complaint is required before triggering analysis)"
        )

    # Image gate — only enforced when patient said they have a visible lesion
    if case.has_visible_lesion:
        image_count = await _count_images(db, case_id)
        if image_count == 0:
            raise BadRequestException(
                message="At least one image must be uploaded before triggering analysis"
            )

    # Idempotency gate (ISS-009)
    if case.ai_status == AiStatus.PROCESSING:
        raise ConflictException(
            message="Analysis is already in progress for this case"
        )
    if case.ai_status == AiStatus.COMPLETED:
        raise ConflictException(
            message="Analysis has already completed for this case"
        )

    # Update status to PROCESSING first — so any concurrent request hits 409
    case.ai_status = AiStatus.PROCESSING
    await db.flush()  # Write to DB before enqueuing

    # Enqueue the correct chain based on whether the patient has a visible lesion
    try:
        result = build_analysis_chain(case_id, case.has_visible_lesion).apply_async()
        task_id = result.id
    except Exception as exc:
        # If Redis is down, roll back the status change and fail gracefully
        case.ai_status = AiStatus.PENDING
        await db.flush()
        logger.error("celery_enqueue_failed", case_id=case_id, error=str(exc))
        raise AIServiceException(
            message="Could not enqueue analysis task. Is Redis running?"
        ) from exc

    case.celery_task_id = task_id
    logger.info("analysis_triggered", case_id=case_id, task_id=task_id)

    return AnalysisAcceptedResponse(
        case_id=case_id,
        task_id=task_id,
    )


# ------------------------------------------------------------------ #
# Status
# ------------------------------------------------------------------ #

async def get_status(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> AnalysisStatusResponse:
    """
    Return the current AI analysis status for a case.

    Available to both the patient and the assigned doctor.
    """
    case = await _get_case_with_access(db, case_id, user)

    completed_at = None
    if case.ai_status == AiStatus.COMPLETED:
        # Use case.updated_at as a proxy for completion time
        completed_at = case.updated_at

    error_message = None
    if case.ai_status == AiStatus.FAILED and case.case_summary:
        # case_summary holds the failure reason when ai_status=FAILED
        error_message = case.case_summary

    recommended_rounds = None
    if case.ai_status == AiStatus.COMPLETED:
        recommended_rounds = case.max_question_rounds

    return AnalysisStatusResponse(
        case_id=case_id,
        ai_status=case.ai_status.value,
        task_id=case.celery_task_id,
        error_message=error_message,
        completed_at=completed_at,
        recommended_rounds=recommended_rounds,
    )


# ------------------------------------------------------------------ #
# Results
# ------------------------------------------------------------------ #

async def get_results(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> AnalysisResultsResponse:
    """
    Return the latest visual description and differential diagnosis for a case.

    Available once ai_status == COMPLETED.
    Returns empty visual_description/differential before then.
    """
    case = await _get_case_with_access(db, case_id, user)

    # Fetch latest visual description (highest round_number)
    vd_result = await db.execute(
        select(VisualDescription)
        .where(VisualDescription.case_id == case_id)
        .order_by(VisualDescription.round_number.desc())
        .limit(1)
    )
    vd = vd_result.scalar_one_or_none()

    # Fetch final differential (is_final=True)
    dd_result = await db.execute(
        select(DifferentialDiagnosis)
        .where(
            DifferentialDiagnosis.case_id == case_id,
            DifferentialDiagnosis.is_final.is_(True),
        )
        .order_by(DifferentialDiagnosis.round_number.desc())
        .limit(1)
    )
    dd = dd_result.scalar_one_or_none()

    visual_out = None
    if vd:
        import json as _json
        try:
            desc = _json.loads(vd.description_json)
        except (ValueError, TypeError):
            desc = {}
        visual_out = VisualDescriptionOut(
            round_number=vd.round_number,
            type_of_lesion=desc.get("type_of_lesion"),
            site=desc.get("site"),
            overall_description=vd.overall_description,
            description_json=vd.description_json,
            created_at=vd.created_at,
        )

    differential_out = None
    if dd:
        differential_out = DifferentialDiagnosisOut(
            round_number=dd.round_number,
            is_final=dd.is_final,
            most_probable_diagnosis=dd.most_probable_diagnosis,
            confidence=dd.confidence,
            diagnosis_json=dd.diagnosis_json,
            created_at=dd.created_at,
        )

    return AnalysisResultsResponse(
        case_id=case_id,
        ai_status=case.ai_status.value,
        visual_description=visual_out,
        differential=differential_out,
        case_summary=case.case_summary,
    )


# ------------------------------------------------------------------ #
# Case AI Query  ("Ask AI" feature on Case Summary screen)
# ------------------------------------------------------------------ #

async def query_case(
    db: AsyncSession,
    user: User,
    case_id: str,
    question: str,
) -> str:
    """
    Answer a free-text patient question using the case's diagnosis,
    differential, and Q&A history as context.

    Available to the patient who owns the case and the assigned doctor.
    Requires AI analysis to be completed.
    """
    import asyncio
    import json as _json

    from src.ai.gemini_client import call_gemini
    from src.ai.prompts.patient_consultation_prompts import PatientConsultationPrompts
    from src.models.message import Message, MessageRole

    case = await _get_case_with_access(db, case_id, user)

    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before you can ask questions about this case."
        )

    # Fetch final differential
    dd_result = await db.execute(
        select(DifferentialDiagnosis)
        .where(
            DifferentialDiagnosis.case_id == case_id,
            DifferentialDiagnosis.is_final.is_(True),
        )
        .order_by(DifferentialDiagnosis.round_number.desc())
        .limit(1)
    )
    dd = dd_result.scalar_one_or_none()

    diagnosis = case.case_title or "Not determined"
    differential_json = dd.diagnosis_json if dd else "{}"
    case_summary = case.case_summary or "Not available"

    # Build Q&A history string from messages
    msgs_result = await db.execute(
        select(Message)
        .where(Message.case_id == case_id)
        .order_by(Message.round_number, Message.question_index)
    )
    messages = list(msgs_result.scalars().all())

    conv_lines: list[str] = []
    for m in messages:
        if m.role == MessageRole.AI:
            try:
                data = _json.loads(m.content)
                if data.get("sentinel"):
                    continue
                q_text = data.get("question", "")
                if q_text:
                    conv_lines.append(f"Q (Round {m.round_number}): {q_text}")
            except (ValueError, TypeError):
                pass
        elif m.role == MessageRole.PATIENT:
            conv_lines.append(f"A: {m.content}")

    conversation_history = "\n".join(conv_lines) if conv_lines else "No Q&A on record."

    prompt = PatientConsultationPrompts.case_query().format(
        diagnosis=diagnosis,
        differential_json=differential_json,
        case_summary=case_summary,
        conversation_history=conversation_history,
        question=question,
    )

    answer = await asyncio.to_thread(call_gemini, prompt)
    return answer.strip()


# ------------------------------------------------------------------ #
# AI Chat Session  ("Ask AI" with session memory)
# ------------------------------------------------------------------ #

async def chat_with_case(
    db: AsyncSession,
    user: User,
    case_id: str,
    message: str,
    session_id: str | None,
) -> AiChatResponse:
    """
    Send a message in a session-managed chat grounded in the case context.

    - If session_id is None → creates a new session.
    - If session_id is provided → loads existing session and continues it.
    - Saves the user message and AI reply to DB so history persists.
    - Gemini receives the full prior conversation so follow-up questions
      are answered in context.
    """
    import asyncio
    import json as _json

    from sqlalchemy.orm import selectinload

    from src.ai.gemini_client import call_gemini
    from src.ai.prompts.patient_consultation_prompts import PatientConsultationPrompts
    from src.models.ai_chat import AiChatMessage, AiChatRole, AiChatSession
    from src.models.message import Message, MessageRole

    # Access check
    case = await _get_case_with_access(db, case_id, user)

    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before you can chat about this case."
        )

    # Load or create session
    prior_messages: list = []
    if session_id:
        sess_result = await db.execute(
            select(AiChatSession)
            .where(AiChatSession.id == session_id, AiChatSession.case_id == case_id)
            .options(selectinload(AiChatSession.messages))
        )
        session = sess_result.scalar_one_or_none()
        if session is None:
            raise BadRequestException(message="Session not found. Omit session_id to start a new one.")
        prior_messages = list(session.messages)
    else:
        session = AiChatSession(
            id=new_uuid(),
            case_id=case_id,
            user_id=user.id,
        )
        db.add(session)
        await db.flush()

    # Build case context (once per request — same for all turns)
    dd_result = await db.execute(
        select(DifferentialDiagnosis)
        .where(DifferentialDiagnosis.case_id == case_id, DifferentialDiagnosis.is_final.is_(True))
        .order_by(DifferentialDiagnosis.round_number.desc())
        .limit(1)
    )
    dd = dd_result.scalar_one_or_none()

    diagnosis = case.case_title or "Not determined"
    differential_json = dd.diagnosis_json if dd else "{}"
    case_summary = case.case_summary or "Not available"

    # Build Q&A history from consultation rounds
    msgs_result = await db.execute(
        select(Message)
        .where(Message.case_id == case_id)
        .order_by(Message.round_number, Message.question_index)
    )
    qa_messages = list(msgs_result.scalars().all())
    qa_lines: list[str] = []
    for m in qa_messages:
        if m.role == MessageRole.AI:
            try:
                data = _json.loads(m.content)
                if data.get("sentinel"):
                    continue
                q_text = data.get("question", "")
                if q_text:
                    qa_lines.append(f"Q (Round {m.round_number}): {q_text}")
            except (ValueError, TypeError):
                pass
        elif m.role == MessageRole.PATIENT:
            qa_lines.append(f"A: {m.content}")

    consultation_history = "\n".join(qa_lines) if qa_lines else "No Q&A on record."

    # Build Gemini conversation: system context + prior chat turns + new message
    system_context = PatientConsultationPrompts.case_query().format(
        diagnosis=diagnosis,
        differential_json=differential_json,
        case_summary=case_summary,
        conversation_history=consultation_history,
        question="{question}",   # placeholder — replaced per turn below
    ).split('The patient has asked:')[0].strip()

    # Construct full prompt with prior chat history + new message
    history_text = ""
    for prior in prior_messages:
        role_label = "Patient" if prior.role == AiChatRole.USER else "AI"
        history_text += f"\n{role_label}: {prior.content}"

    full_prompt = (
        f"{system_context}\n\n"
        f"{'Prior conversation:' + history_text if history_text else ''}\n\n"
        f"Patient: {message}\n\n"
        "Answer the patient's latest question in the context of the full conversation above."
    )

    # Call Gemini
    answer = await asyncio.to_thread(call_gemini, full_prompt)
    answer = answer.strip()

    # Persist both turns
    user_msg = AiChatMessage(
        id=new_uuid(),
        session_id=session.id,
        role=AiChatRole.USER,
        content=message,
    )
    ai_msg = AiChatMessage(
        id=new_uuid(),
        session_id=session.id,
        role=AiChatRole.ASSISTANT,
        content=answer,
    )
    db.add(user_msg)
    db.add(ai_msg)
    await db.flush()

    return AiChatResponse(session_id=session.id, answer=answer)


async def get_chat_history(
    db: AsyncSession,
    user: User,
    case_id: str,
    session_id: str,
) -> AiChatHistoryResponse:
    """
    Return the full message history for a chat session.
    """
    from sqlalchemy.orm import selectinload
    from src.models.ai_chat import AiChatSession

    # Access check on the case
    await _get_case_with_access(db, case_id, user)

    sess_result = await db.execute(
        select(AiChatSession)
        .where(AiChatSession.id == session_id, AiChatSession.case_id == case_id)
        .options(selectinload(AiChatSession.messages))
    )
    session = sess_result.scalar_one_or_none()
    if session is None:
        raise BadRequestException(message="Chat session not found.")

    return AiChatHistoryResponse(
        session_id=session.id,
        case_id=case_id,
        messages=[
            ChatMessageOut(
                id=m.id,
                role=m.role.value,
                content=m.content,
                created_at=m.created_at,
            )
            for m in session.messages
        ],
    )
