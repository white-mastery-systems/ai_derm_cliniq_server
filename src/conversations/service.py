"""
conversations/service.py — Chat Business Logic
===============================================

Three operations:
1. trigger_questions   — validate + enqueue generate_questions_task
2. submit_answers      — save answer messages + enqueue refine_analysis_task
3. get_history         — return full conversation as grouped rounds

PRE-CONDITIONS
--------------
trigger_questions:
  - ai_status must be COMPLETED (analysis done before Q&A starts)
  - No AI messages exist yet for the current round (idempotency)
  - Only patient can trigger

submit_answers:
  - AI questions exist for current round (questions were generated)
  - No patient answers exist yet for current round (idempotency)
  - answer count must match question count for the round
  - Only patient can submit

ACCESS CONTROL
--------------
- trigger_questions / submit_answers: patient only
- get_history: patient or assigned doctor
"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.conversations.schemas import (
    AnswersAcceptedResponse,
    ConversationHistoryResponse,
    FinishChatResponse,
    QuestionsGeneratedResponse,
    RoundOut,
    QuestionItem,
    MessageOut,
    SubmitAnswersRequest,
)
from src.exceptions import (
    AIServiceException,
    BadRequestException,
    CaseNotFoundException,
    ConflictException,
    ForbiddenException,
)
from src.workers.tasks.questions import generate_questions_task, refine_analysis_task
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import AiStatus, Case
from src.models.message import Message, MessageRole
from src.models.user import User, UserRole

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

async def _get_case_with_access(db: AsyncSession, case_id: str, user: User) -> Case:
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    if user.role == UserRole.PATIENT and case.patient_id != user.id:
        raise CaseNotFoundException()
    if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
        raise CaseNotFoundException()
    return case


async def _get_messages_for_round(
    db: AsyncSession, case_id: str, round_number: int
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.case_id == case_id, Message.round_number == round_number)
        .order_by(Message.question_index, Message.created_at)
    )
    return list(result.scalars().all())


async def _get_all_messages(db: AsyncSession, case_id: str) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.case_id == case_id)
        .order_by(Message.round_number, Message.question_index, Message.created_at)
    )
    return list(result.scalars().all())


def _parse_question(msg: Message) -> QuestionItem | None:
    """Parse a Message(role=AI) into a QuestionItem. Returns None on parse error."""
    try:
        data = json.loads(msg.content)
        return QuestionItem(
            question=data.get("question", ""),
            answer_options=data.get("answer_options", []),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------ #
# 1. Trigger questions
# ------------------------------------------------------------------ #

async def trigger_questions(
    db: AsyncSession,
    patient: User,
    case_id: str,
) -> QuestionsGeneratedResponse:
    """
    Validate the case and enqueue generate_questions_task.

    Only valid at round 0 (initial questions). Subsequent rounds are
    triggered automatically by refine_analysis_task after patient answers.
    """
    if patient.role != UserRole.PATIENT:
        raise ForbiddenException(message="Only the patient can trigger question generation")

    case = await _get_case_with_access(db, case_id, patient)

    # Require completed AI analysis
    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before questions can be generated. "
                    f"Current status: {case.ai_status.value}"
        )

    # Only allow explicit trigger for round 0
    if case.question_round > 0:
        raise ConflictException(
            message="Questions for subsequent rounds are generated automatically after submitting answers"
        )

    # Idempotency: don't re-generate if questions already exist for round 0
    existing = await _get_messages_for_round(db, case_id, 0)
    ai_msgs = [m for m in existing if m.role == MessageRole.AI]
    if ai_msgs:
        raise ConflictException(message="Questions for round 0 have already been generated")

    # Enqueue task
    try:
        result = generate_questions_task.delay(case_id)
        task_id = result.id
    except Exception as exc:
        logger.error("generate_questions_enqueue_failed", case_id=case_id, error=str(exc))
        raise AIServiceException(
            message="Could not enqueue question generation task. Is Redis running?"
        ) from exc

    logger.info("questions_triggered", case_id=case_id, task_id=task_id)
    return QuestionsGeneratedResponse(
        case_id=case_id,
        round_number=case.question_round,
        task_id=task_id,
    )


# ------------------------------------------------------------------ #
# 2. Submit answers
# ------------------------------------------------------------------ #

async def submit_answers(
    db: AsyncSession,
    patient: User,
    case_id: str,
    request: SubmitAnswersRequest,
) -> AnswersAcceptedResponse:
    """
    Save patient answers for the current round and enqueue re-analysis.

    Validates:
    - Questions exist for current round
    - Answers haven't already been submitted (idempotency)
    - Answer count matches question count
    """
    if patient.role != UserRole.PATIENT:
        raise ForbiddenException(message="Only the patient can submit answers")

    case = await _get_case_with_access(db, case_id, patient)

    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before submitting answers"
        )

    current_round = case.question_round
    messages = await _get_messages_for_round(db, case_id, current_round)

    ai_msgs = [m for m in messages if m.role == MessageRole.AI]
    if not ai_msgs:
        raise BadRequestException(
            message="No questions have been generated for the current round yet. "
                    "Wait for question generation to complete."
        )

    # Idempotency: reject if answers already submitted this round
    patient_msgs = [m for m in messages if m.role == MessageRole.PATIENT]
    if patient_msgs:
        raise ConflictException(
            message=f"Answers for round {current_round} have already been submitted"
        )

    # Validate answer count matches question count
    if len(request.answers) != len(ai_msgs):
        raise BadRequestException(
            message=f"Expected {len(ai_msgs)} answers (one per question), got {len(request.answers)}"
        )

    # Save answer messages
    for ans in request.answers:
        msg = Message(
            id=new_uuid(),
            case_id=case_id,
            role=MessageRole.PATIENT,
            content=ans.answer,
            round_number=current_round,
            question_index=ans.question_index,
        )
        db.add(msg)

    await db.flush()

    # Enqueue re-analysis
    try:
        result = refine_analysis_task.delay(case_id)
        task_id = result.id
    except Exception as exc:
        logger.error("refine_analysis_enqueue_failed", case_id=case_id, error=str(exc))
        raise AIServiceException(
            message="Could not enqueue re-analysis task. Is Redis running?"
        ) from exc

    logger.info("answers_submitted", case_id=case_id, round=current_round, task_id=task_id)
    return AnswersAcceptedResponse(
        case_id=case_id,
        round_number=current_round,
        task_id=task_id,
    )


# ------------------------------------------------------------------ #
# 3. Finish conversation early ("Finish Now" button)
# ------------------------------------------------------------------ #

async def finish_conversation(
    db: AsyncSession,
    patient: User,
    case_id: str,
) -> FinishChatResponse:
    """
    Skip remaining Q&A rounds and mark the conversation as complete.

    Sets question_round = max_question_rounds so that is_complete becomes True.
    Idempotent — safe to call if conversation is already complete.

    Only the patient who owns the case can finish early.
    AI analysis must have completed before finish can be called.
    """
    if patient.role != UserRole.PATIENT:
        raise ForbiddenException(message="Only the patient can finish the conversation")

    case = await _get_case_with_access(db, case_id, patient)

    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before the conversation can be finished. "
                    f"Current status: {case.ai_status.value}"
        )

    already_complete = case.question_round >= case.max_question_rounds
    if not already_complete:
        case.question_round = case.max_question_rounds
        await db.flush()
        logger.info("conversation_finished_early", case_id=case_id, patient_id=patient.id)
    else:
        logger.info("conversation_already_complete", case_id=case_id)

    return FinishChatResponse(
        case_id=case_id,
        question_round=case.question_round,
        max_rounds=case.max_question_rounds,
        is_complete=True,
    )


# ------------------------------------------------------------------ #
# 4. Get conversation history
# ------------------------------------------------------------------ #

async def get_history(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> ConversationHistoryResponse:
    """
    Return the full conversation history grouped by round.

    Available to both the patient and the assigned doctor.
    """
    case = await _get_case_with_access(db, case_id, user)
    all_messages = await _get_all_messages(db, case_id)

    # Build round groups
    rounds_map: dict[int, dict] = {}
    for msg in all_messages:
        rn = msg.round_number
        if rn not in rounds_map:
            rounds_map[rn] = {"ai": [], "patient": {}}
        if msg.role == MessageRole.AI:
            rounds_map[rn]["ai"].append(msg)
        elif msg.role == MessageRole.PATIENT:
            rounds_map[rn]["patient"][msg.question_index] = msg.content

    rounds_out: list[RoundOut] = []
    for rn in sorted(rounds_map.keys()):
        ai_msgs = sorted(rounds_map[rn]["ai"], key=lambda m: m.question_index or 0)
        questions = [q for msg in ai_msgs if (q := _parse_question(msg)) is not None]
        answers = [
            rounds_map[rn]["patient"].get(i, "")
            for i in range(len(ai_msgs))
        ]
        rounds_out.append(RoundOut(
            round_number=rn,
            questions=questions,
            answers=answers,
        ))

    raw_messages = [
        MessageOut(
            id=m.id,
            role=m.role.value,
            content=m.content,
            round_number=m.round_number,
            question_index=m.question_index,
            created_at=m.created_at,
        )
        for m in all_messages
    ]

    is_complete = case.question_round >= case.max_question_rounds

    return ConversationHistoryResponse(
        case_id=case_id,
        current_round=case.question_round,
        max_rounds=case.max_question_rounds,
        is_complete=is_complete,
        rounds=rounds_out,
        raw_messages=raw_messages,
    )
