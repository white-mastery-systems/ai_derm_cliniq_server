"""
ai/controller.py — AI Analysis HTTP Endpoints
==============================================

All routes prefixed /api/v1/cases/{case_id}/ai (set in src/api.py).

ROUTES
------
POST   /analyze   → Patient: trigger AI analysis chain (202 Accepted)
GET    /status    → Patient/Doctor: poll analysis progress
GET    /results   → Patient/Doctor: fetch visual description + differential

WHY 202 ACCEPTED?
-----------------
Analysis takes 15-60 seconds. Returning 202 immediately tells the
Flutter app "your request was accepted, come back and poll /status".
The app polls every 3-5 seconds until ai_status is 'completed' or 'failed'.

PATIENT-ONLY TRIGGER
--------------------
Only the patient who owns the case can trigger analysis. Doctors read
results but never initiate analysis — they review what the AI produces.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai import service
from src.ai.schemas import (
    AiChatHistoryResponse,
    AiChatRequest,
    AiChatResponse,
    AnalysisAcceptedResponse,
    AnalysisResultsResponse,
    AnalysisStatusResponse,
    CaseQueryRequest,
    CaseQueryResponse,
)
from src.auth.dependencies import get_current_user, require_patient
from src.database.core import get_async_session
from src.models.user import User

router = APIRouter()


@router.post(
    "/analyze",
    response_model=AnalysisAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger AI analysis for a case",
)
async def trigger_analysis(
    case_id: str,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> AnalysisAcceptedResponse:
    return await service.trigger_analysis(db, patient, case_id)


@router.get(
    "/status",
    response_model=AnalysisStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Poll AI analysis progress",
)
async def get_analysis_status(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AnalysisStatusResponse:
    return await service.get_status(db, user, case_id)


@router.get(
    "/results",
    response_model=AnalysisResultsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get AI analysis results (visual description + differential)",
)
async def get_analysis_results(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AnalysisResultsResponse:
    return await service.get_results(db, user, case_id)


@router.post(
    "/query",
    response_model=CaseQueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Ask AI a question about this case",
    description=(
        "Free-text question about the case — used by the 'Ask AI' input and quick prompts "
        "on the Case Summary screen. Answers are grounded in the case's diagnosis, "
        "differential, and Q&A history. Requires AI analysis to be completed."
    ),
)
async def query_case(
    case_id: str,
    body: CaseQueryRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> CaseQueryResponse:
    answer = await service.query_case(db, user, case_id, body.question)
    return CaseQueryResponse(case_id=case_id, question=body.question, answer=answer)


@router.post(
    "/chat",
    response_model=AiChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Send a message in a session-managed AI chat",
    description=(
        "Stateful Ask AI — each reply is aware of the full prior conversation.\n\n"
        "**First message:** omit `session_id` — a new session is created and its ID "
        "is returned.\n\n"
        "**Follow-up messages:** include the `session_id` from the previous response "
        "to continue the same conversation.\n\n"
        "Each session is scoped to one case + one user. "
        "Requires AI analysis to be completed."
    ),
)
async def chat_with_case(
    case_id: str,
    body: AiChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AiChatResponse:
    return await service.chat_with_case(db, user, case_id, body.message, body.session_id)


@router.get(
    "/chat/{session_id}",
    response_model=AiChatHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get full message history for a chat session",
)
async def get_chat_history(
    case_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> AiChatHistoryResponse:
    return await service.get_chat_history(db, user, case_id, session_id)
