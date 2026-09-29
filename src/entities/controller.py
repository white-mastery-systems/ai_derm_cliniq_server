"""
entities/controller.py — SNOMED Entity HTTP Endpoints
======================================================

Route nested under /api/v1/cases/{case_id}/entities (set in src/api.py).

ROUTES
------
GET /entities  → Returns SNOMED CT coded entities for the case's
                 final AI differential diagnosis

ACCESS
------
Patient who owns the case, OR the assigned doctor.
Requires AI analysis to be completed (final differential must exist).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.database.core import get_async_session
from src.entities import service
from src.entities.schemas import CaseEntitiesResponse
from src.models.user import User

router = APIRouter()


@router.get(
    "/entities",
    response_model=CaseEntitiesResponse,
    status_code=status.HTTP_200_OK,
    summary="Get SNOMED CT coded entities for a case",
)
async def get_entities(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> CaseEntitiesResponse:
    """
    Returns the case's AI differential diagnoses enriched with SNOMED CT codes.

    Each entity includes:
    - diagnosis_text: AI-generated diagnosis string
    - snomed_code: SNOMED CT concept ID (e.g. "9014002")
    - snomed_term: Official SNOMED preferred term (e.g. "Psoriasis (disorder)")
    - is_most_probable: True for the primary diagnosis
    - confidence: AI confidence or likelihood

    Results are cached in the snomed_mappings table — the same diagnosis
    string is only looked up against the CSV once, ever.

    Returns 404 if AI analysis has not completed yet.
    """
    return await service.get_entities(db, user, case_id)
