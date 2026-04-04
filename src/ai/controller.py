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
    AnalysisAcceptedResponse,
    AnalysisResultsResponse,
    AnalysisStatusResponse,
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
