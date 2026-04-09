"""
dependents/controller.py — Dependent HTTP Endpoints
====================================================

Routes prefixed /api/v1/users/me/dependents (set in src/api.py).

ROUTES
------
GET    /         → Patient: list all saved dependents with last visit info
POST   /         → Patient: save a new dependent ("+ Add new patient" form)
PATCH  /{dep_id} → Patient: update an existing dependent's details

PATIENT-ONLY
------------
All three endpoints require the caller to be a patient. Doctors and admins
do not manage dependent lists.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import require_patient
from src.database.core import get_async_session
from src.dependents import service
from src.dependents.schemas import (
    DependentCreateRequest,
    DependentListResponse,
    DependentResponse,
    DependentUpdateRequest,
)
from src.models.user import User

router = APIRouter()


@router.get(
    "",
    response_model=DependentListResponse,
    status_code=status.HTTP_200_OK,
    summary="List saved dependents with last visit info",
)
async def list_dependents(
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> DependentListResponse:
    """
    Returns all dependents the patient has saved, each including:
    - name, relationship, age, gender
    - last_visit_date and last_visit_diagnosis from their most recent case
    """
    return await service.list_dependents(db, patient)


@router.post(
    "",
    response_model=DependentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a new dependent ('+ Add new patient')",
)
async def create_dependent(
    request: DependentCreateRequest,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> DependentResponse:
    return await service.create_dependent(db, patient, request)


@router.patch(
    "/{dependent_id}",
    response_model=DependentResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a saved dependent's details",
)
async def update_dependent(
    dependent_id: str,
    request: DependentUpdateRequest,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> DependentResponse:
    return await service.update_dependent(db, patient, dependent_id, request)
