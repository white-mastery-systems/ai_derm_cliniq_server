"""
entities/service.py — SNOMED Entity Lookup Business Logic
==========================================================

One operation: get_entities(db, user, case_id)

Fetches the case's final differential diagnosis, maps each diagnosis
string to a SNOMED CT code (via CSV lookup + DB cache), and returns
structured clinical entities.

LOOKUP PIPELINE
---------------
For each diagnosis string:
1. Check snomed_mappings table (exact text match) — cache hit → skip CSV
2. CSV lookup via entities/snomed.py → match against 31K dermatology terms
3. Write result to snomed_mappings (even if no match found — so we don't
   retry the same string on every request)

NULL SNOMED
-----------
If no SNOMED match is found, snomed_code and snomed_term are None.
The entity is still returned — the diagnosis text is always present.

ACCESS RULES
------------
Patient who owns the case, OR the assigned doctor.
Others get 404 (case enumeration prevention).

REQUIRES COMPLETED AI
---------------------
The entities endpoint only makes sense once AI analysis is done and
a final differential exists. Returns 404 if no differential found.
"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.entities.schemas import CaseEntitiesResponse, ClinicalEntity
from src.entities.snomed import lookup_snomed
from src.exceptions import CaseNotFoundException, ForbiddenException, NotFoundException
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import Case
from src.models.differential_diagnosis import DifferentialDiagnosis
from src.models.snomed_mapping import SnomedMapping
from src.models.user import User, UserRole

logger = get_logger(__name__)


async def _get_snomed(db: AsyncSession, diagnosis_text: str) -> tuple[str | None, str | None]:
    """
    Get SNOMED code for a diagnosis text.

    1. Check DB cache first
    2. Fall back to CSV lookup
    3. Save result to cache (miss or hit)
    """
    # Check cache
    result = await db.execute(
        select(SnomedMapping).where(SnomedMapping.diagnosis_text == diagnosis_text)
    )
    cached = result.scalar_one_or_none()
    if cached is not None:
        return cached.snomed_code, cached.snomed_term

    # CSV lookup
    code, term = lookup_snomed(diagnosis_text)

    # Cache the result (even None — avoids repeated CSV scans)
    mapping = SnomedMapping(
        id=new_uuid(),
        diagnosis_text=diagnosis_text,
        snomed_code=code,
        snomed_term=term,
    )
    db.add(mapping)
    # flush so the cache row is written, but don't fail the whole request if it errors
    try:
        await db.flush()
    except Exception as exc:
        logger.warning("snomed_cache_write_failed", diagnosis=diagnosis_text, error=str(exc))

    return code, term


async def get_entities(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> CaseEntitiesResponse:
    """
    Return SNOMED-coded clinical entities for the case's final differential.

    Access: patient (owns case) or assigned doctor.
    """
    result = await db.execute(
        select(Case)
        .where(Case.id == case_id)
        .options(
            selectinload(Case.differential_diagnoses),
            selectinload(Case.doctor_review),
        )
    )
    case = result.scalar_one_or_none()

    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if user.role == UserRole.PATIENT and case.patient_id != user.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
        raise ForbiddenException(message="You are not assigned to this case.")

    # Find final differential (is_final=True) or fall back to latest
    final_dd = next(
        (d for d in case.differential_diagnoses if d.is_final), None
    ) or (
        max(case.differential_diagnoses, key=lambda d: d.round_number)
        if case.differential_diagnoses else None
    )

    # Doctor flow: no DifferentialDiagnosis rows — use confirmed_diagnosis from review
    if final_dd is None:
        dr = case.doctor_review
        confirmed_raw = dr.confirmed_diagnosis if dr else None
        if not confirmed_raw:
            raise NotFoundException(
                message="No differential diagnosis found for this case. "
                        "AI analysis must complete before entities can be retrieved."
            )
        try:
            confirmed_list: list[str] = json.loads(confirmed_raw)
        except (json.JSONDecodeError, TypeError):
            confirmed_list = [confirmed_raw]

        entities: list[ClinicalEntity] = []
        for i, text in enumerate(confirmed_list):
            if not text:
                continue
            code, term = await _get_snomed(db, text)
            entities.append(ClinicalEntity(
                diagnosis_text=text,
                snomed_code=code,
                snomed_term=term,
                is_most_probable=(i == 0),
                confidence=None,
                key_supporting_features=None,
            ))

        logger.info(
            "entities_retrieved_from_review",
            case_id=case_id,
            entity_count=len(entities),
            snomed_hits=sum(1 for e in entities if e.snomed_code),
        )
        return CaseEntitiesResponse(
            case_id=case_id,
            differential_round=0,
            entities=entities,
        )

    # Parse diagnosis JSON
    try:
        diag_data = json.loads(final_dd.diagnosis_json)
    except (json.JSONDecodeError, TypeError):
        diag_data = {}

    most_probable = diag_data.get("most_probable_diagnosis", {})
    differentials = diag_data.get("differential_diagnoses", [])
    confidence = diag_data.get("confidence in answer") or diag_data.get("confidence")

    entities = []

    # Most probable diagnosis first
    if isinstance(most_probable, dict) and most_probable.get("diagnosis"):
        text = most_probable["diagnosis"]
        code, term = await _get_snomed(db, text)
        entities.append(ClinicalEntity(
            diagnosis_text=text,
            snomed_code=code,
            snomed_term=term,
            is_most_probable=True,
            confidence=str(confidence) if confidence else None,
            key_supporting_features=most_probable.get("key_supporting_features") or None,
        ))

    # Differential diagnoses
    if isinstance(differentials, list):
        for diff in differentials:
            if not isinstance(diff, dict):
                continue
            text = diff.get("diagnosis", "")
            if not text:
                continue
            code, term = await _get_snomed(db, text)
            entities.append(ClinicalEntity(
                diagnosis_text=text,
                snomed_code=code,
                snomed_term=term,
                is_most_probable=False,
                confidence=diff.get("likelihood"),
                key_supporting_features=diff.get("key_supporting_features") or None,
            ))

    logger.info(
        "entities_retrieved",
        case_id=case_id,
        entity_count=len(entities),
        snomed_hits=sum(1 for e in entities if e.snomed_code),
    )

    return CaseEntitiesResponse(
        case_id=case_id,
        differential_round=final_dd.round_number,
        entities=entities,
    )
