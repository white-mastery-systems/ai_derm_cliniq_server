"""
dependents/schemas.py — Dependent Request & Response Models
============================================================

ENDPOINTS
---------
GET  /api/v1/users/me/dependents         → list saved dependents
POST /api/v1/users/me/dependents         → create new dependent
PATCH /api/v1/users/me/dependents/{id}   → update dependent

"SOMEONE ELSE" FLOW (Figma)
----------------------------
Home screen → "Someone else" → "Select the patient" screen → shows this list.
Each card shows: name, age, relationship, last_visit_date, last_visit_diagnosis.

"+ Add new patient" opens "Add Patient details" form:
  - Full Name
  - Age (integer — converted to approximate DOB server-side)
  - Sex
  - Relationship

INTEGRATION WITH CASE CREATION
--------------------------------
When a patient selects an existing dependent from the list and starts a consultation,
POST /cases is called with:
  { is_for_self: false, dependent_id: "<uuid>" }

The server loads the Dependent row and copies its data into the case's inline
dependent_* columns, keeping all existing case queries unchanged.
"""

from datetime import date, datetime

from pydantic import BaseModel, Field


class DependentCreateRequest(BaseModel):
    """
    POST /api/v1/users/me/dependents

    The 'Add new patient' form collects Age (integer) rather than a full date of
    birth. The server converts age → approximate DOB (January 1st of birth year).
    """
    name: str = Field(min_length=2, max_length=255)
    age: int = Field(ge=0, le=120, description="Patient's current age in years")
    gender: str = Field(
        max_length=50,
        description="Male | Female | Other | Prefer not to say",
    )


class DependentUpdateRequest(BaseModel):
    """PATCH /api/v1/users/me/dependents/{id} — all fields optional."""
    name: str | None = Field(default=None, min_length=2, max_length=255)
    age: int | None = Field(default=None, ge=0, le=120)
    gender: str | None = Field(default=None, max_length=50)


class DependentResponse(BaseModel):
    """Single dependent returned in list and after create/update."""
    id: str
    name: str
    age: int | None = None                       # Calculated from date_of_birth
    gender: str | None = None
    date_of_birth: date | None = None
    last_visit_date: datetime | None = None      # Most recent case created_at
    last_visit_diagnosis: str | None = None      # case_title of most recent case

    model_config = {"from_attributes": True}


class DependentListResponse(BaseModel):
    """GET /api/v1/users/me/dependents"""
    dependents: list[DependentResponse]
    total: int
