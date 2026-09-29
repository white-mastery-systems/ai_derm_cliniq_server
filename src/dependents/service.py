"""
dependents/service.py — Dependent Business Logic
=================================================

Three operations:
1. list_dependents   — return all saved dependents with last visit info
2. create_dependent  — save a new dependent for the patient
3. update_dependent  — update an existing dependent's details

LAST VISIT QUERY
----------------
For each dependent, we join to the cases table to find the most recent
case (by created_at) where case.dependent_id = dependent.id.
We pull case_title as the last_visit_diagnosis.

AGE ↔ DOB CONVERSION
---------------------
The "Add new patient" form sends age (int). We convert to an approximate
DOB of January 1st of the birth year:
  dob = date(today.year - age, 1, 1)
When returning a dependent, we calculate current age from DOB:
  age = (today - dob).days // 365
"""

from datetime import date, datetime

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependents.schemas import (
    DependentCreateRequest,
    DependentListResponse,
    DependentResponse,
    DependentUpdateRequest,
)
from src.exceptions import BadRequestException, ForbiddenException
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import Case
from src.models.dependent import Dependent
from src.models.user import User, UserRole

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _age_from_dob(dob: date | None) -> int | None:
    if dob is None:
        return None
    today = date.today()
    return (today - dob).days // 365


def _dob_from_age(age: int) -> date:
    """Approximate DOB: January 1st of the birth year."""
    birth_year = date.today().year - age
    return date(birth_year, 1, 1)


def _to_response(
    dep: Dependent,
    last_visit_date: datetime | None = None,
    last_visit_diagnosis: str | None = None,
) -> DependentResponse:
    return DependentResponse(
        id=dep.id,
        name=dep.name,
        age=_age_from_dob(dep.date_of_birth),
        gender=dep.gender,
        date_of_birth=dep.date_of_birth,
        last_visit_date=last_visit_date,
        last_visit_diagnosis=last_visit_diagnosis,
    )


# ------------------------------------------------------------------ #
# List
# ------------------------------------------------------------------ #

async def list_dependents(
    db: AsyncSession,
    patient: User,
) -> DependentListResponse:
    """
    Return all saved dependents for the patient, each enriched with
    the most recent case date and diagnosis title.
    """
    result = await db.execute(
        select(Dependent)
        .where(Dependent.patient_id == patient.id)
        .order_by(Dependent.created_at.desc())
    )
    dependents = list(result.scalars().all())

    if not dependents:
        return DependentListResponse(dependents=[], total=0)

    # Batch: fetch most recent case per dependent in one query
    dep_ids = [d.id for d in dependents]
    cases_result = await db.execute(
        select(Case)
        .where(Case.dependent_id.in_(dep_ids))
        .order_by(Case.dependent_id, desc(Case.created_at))
    )
    all_cases = list(cases_result.scalars().all())

    # Keep only the most recent case per dependent_id
    last_case: dict[str, Case] = {}
    for c in all_cases:
        if c.dependent_id and c.dependent_id not in last_case:
            last_case[c.dependent_id] = c

    items = [
        _to_response(
            dep,
            last_visit_date=last_case[dep.id].created_at if dep.id in last_case else None,
            last_visit_diagnosis=last_case[dep.id].case_title if dep.id in last_case else None,
        )
        for dep in dependents
    ]
    return DependentListResponse(dependents=items, total=len(items))


# ------------------------------------------------------------------ #
# Create
# ------------------------------------------------------------------ #

async def create_dependent(
    db: AsyncSession,
    patient: User,
    request: DependentCreateRequest,
) -> DependentResponse:
    """Save a new dependent for the patient (from '+ Add new patient' form)."""
    if patient.role != UserRole.PATIENT:
        raise ForbiddenException(message="Only patients can manage dependents")

    dep = Dependent(
        id=new_uuid(),
        patient_id=patient.id,
        name=request.name,
        date_of_birth=_dob_from_age(request.age),
        gender=request.gender,
    )
    db.add(dep)
    await db.flush()

    logger.info("dependent_created", dependent_id=dep.id, patient_id=patient.id)
    return _to_response(dep)


# ------------------------------------------------------------------ #
# Update
# ------------------------------------------------------------------ #

async def update_dependent(
    db: AsyncSession,
    patient: User,
    dependent_id: str,
    request: DependentUpdateRequest,
) -> DependentResponse:
    """Update name, age, gender, or relationship of a saved dependent."""
    if patient.role != UserRole.PATIENT:
        raise ForbiddenException(message="Only patients can manage dependents")

    result = await db.execute(
        select(Dependent).where(Dependent.id == dependent_id)
    )
    dep = result.scalar_one_or_none()

    if dep is None or dep.patient_id != patient.id:
        raise BadRequestException(message=f"No dependent found with id: {dependent_id}")

    if request.name is not None:
        dep.name = request.name
    if request.age is not None:
        dep.date_of_birth = _dob_from_age(request.age)
    if request.gender is not None:
        dep.gender = request.gender

    await db.flush()
    logger.info("dependent_updated", dependent_id=dep.id, patient_id=patient.id)
    return _to_response(dep)
