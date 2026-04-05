"""
entities/schemas.py — SNOMED Entity Response Models
====================================================

ONE ENDPOINT
------------
GET /api/v1/cases/{case_id}/entities
    → Returns SNOMED CT coded entities for the case's final differential diagnosis

WHAT IS AN "ENTITY"?
--------------------
A clinical entity is a diagnosis string from the AI output, enriched
with a SNOMED CT code and official term.

Example entity:
    {
        "diagnosis_text": "Psoriasis",
        "snomed_code": "9014002",
        "snomed_term": "Psoriasis (disorder)",
        "is_most_probable": true,
        "confidence": "high"
    }

CACHE SOURCE
------------
Codes are cached in the snomed_mappings table to avoid re-loading the
CSV on every request for the same diagnosis string.
"""

from pydantic import BaseModel


class ClinicalEntity(BaseModel):
    """One SNOMED-coded diagnosis entity."""
    diagnosis_text: str
    snomed_code: str | None
    snomed_term: str | None
    is_most_probable: bool
    confidence: str | None   # "high" | "medium" | "low" | None


class CaseEntitiesResponse(BaseModel):
    """Returned by GET /cases/{case_id}/entities."""
    case_id: str
    differential_round: int
    entities: list[ClinicalEntity]
