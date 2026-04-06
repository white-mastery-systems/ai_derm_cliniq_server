"""
conversations/controller.py — Chat HTTP Endpoints
==================================================

All routes prefixed /api/v1/cases/{case_id}/chat (set in src/api.py).

ROUTES
------
POST   /questions   → Patient: trigger AI question generation (202)
POST   /answers     → Patient: submit answers for current round (202)
GET    /            → Patient/Doctor: full conversation history

FLOW
----
1. Patient calls POST /questions after AI analysis completes.
2. Client polls GET / until questions appear (round_number messages visible).
3. Patient selects answers and calls POST /answers.
4. refine_analysis_task re-analyses + auto-enqueues next questions.
5. Client polls GET / to see new questions appear.
6. Repeat until is_complete=True in the GET / response.
"""

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_patient
from src.conversations import service
from src.conversations.schemas import (
    AnswersAcceptedResponse,
    ConversationHistoryResponse,
    QuestionsGeneratedResponse,
    SubmitAnswersRequest,
)
from src.conversations.stream import chat_stream_generator
from src.database.core import get_async_session
from src.models.user import User

router = APIRouter()


@router.post(
    "/questions",
    response_model=QuestionsGeneratedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate follow-up questions for the current round",
)
async def trigger_questions(
    case_id: str,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> QuestionsGeneratedResponse:
    return await service.trigger_questions(db, patient, case_id)


@router.post(
    "/answers",
    response_model=AnswersAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit patient answers and trigger re-analysis",
)
async def submit_answers(
    case_id: str,
    body: SubmitAnswersRequest,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> AnswersAcceptedResponse:
    return await service.submit_answers(db, patient, case_id, body)


@router.get(
    "",
    response_model=ConversationHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get full conversation history grouped by round",
)
async def get_history(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> ConversationHistoryResponse:
    return await service.get_history(db, user, case_id)


@router.get(
    "/stream",
    summary="Real-time SSE stream for chat events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Server-Sent Events stream",
            "content": {"text/event-stream": {}},
        }
    },
)
async def stream_chat(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> StreamingResponse:
    """
    Open a persistent SSE connection to receive real-time chat events.

    Events emitted:
    - **status_update** — ai_status changed (pending → processing → completed)
    - **questions_ready** — AI questions appeared for the current round
    - **round_complete** — Patient answers recorded, new round starting
    - **conversation_complete** — All rounds done, case summary ready
    - **ping** — Heartbeat every 15 seconds (keep-alive)
    - **error** — Fatal error or stream timeout (5 minutes)

    The Flutter app opens this once after triggering analysis and keeps
    it open until `conversation_complete` or `error` is received.
    Answers are submitted via the existing `POST /answers` endpoint — not
    via this stream.
    """
    return StreamingResponse(
        chat_stream_generator(db, case_id, user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )
