"""
cases/service.py — Case Business Logic
========================================

All functions are async and receive an AsyncSession.

OWNERSHIP RULES (enforced here, not in controller)
----------------------------------------------------
- A patient can only read/update/delete their OWN cases.
- A doctor can only read cases assigned to them.
- Assigning a doctor (PATCH /{id}/assign) is idempotent:
  if already assigned to the same doctor → no-op.
  If already assigned to a DIFFERENT doctor → 403.

SOFT DELETE
-----------
Cases are never hard-deleted. We set ai_status=FAILED to mark cancellation.
The image files in GCS remain (deleting them is a separate admin task).

CONSENT IMMUTABILITY
--------------------
consent_ai_analysis and consent_research are set at case creation and are immutable.
There is no separate consent endpoint — consent is captured in POST /cases.

PAGINATION
----------
Uses simple offset pagination: page=1 returns rows 0..page_size-1.
"""

from datetime import datetime, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.cases.schemas import (
    AssessmentDepthRequest,
    AssessmentDepthResponse,
    CaseCreateRequest,
    CaseResponse,
    CaseSummaryResponse,
    CaseUpdateRequest,
    DoctorStatsResponse,
    PaginatedCasesResponse,
    RedFlagsResponse,
)
from src.exceptions import (
    BadRequestException,
    CaseNotFoundException,
    ForbiddenException,
)
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import AiStatus, Case, ClinicalStatus, ConsultationType, RedFlagStatus
from src.models.case_image import CaseImage
from src.models.user import User, UserRole

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Internal helpers
# ------------------------------------------------------------------ #

def _to_case_response(case: Case, image_count: int = 0) -> CaseResponse:
    return CaseResponse(
        id=case.id,
        patient_id=case.patient_id,
        doctor_id=case.doctor_id,
        consultation_type=case.consultation_type.value,
        has_visible_lesion=case.has_visible_lesion,
        consent_ai_analysis=case.consent_ai_analysis,
        consent_ai_analysis_at=case.consent_ai_analysis_at,
        consent_research=case.consent_research,
        is_for_self=case.is_for_self,
        dependent_name=case.dependent_name,
        dependent_relationship=case.dependent_relationship,
        dependent_dob=case.dependent_dob,
        dependent_gender=case.dependent_gender,
        ai_status=case.ai_status.value,
        clinical_status=case.clinical_status.value,
        presenting_complaint=case.presenting_complaint,
        case_summary=case.case_summary,
        celery_task_id=case.celery_task_id,
        question_round=case.question_round,
        max_question_rounds=case.max_question_rounds,
        image_count=image_count,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


def _to_summary_response(case: Case, image_count: int = 0) -> CaseSummaryResponse:
    return CaseSummaryResponse(
        id=case.id,
        consultation_type=case.consultation_type.value,
        ai_status=case.ai_status.value,
        clinical_status=case.clinical_status.value,
        is_for_self=case.is_for_self,
        dependent_name=case.dependent_name,
        consent_ai_analysis=case.consent_ai_analysis,
        has_visible_lesion=case.has_visible_lesion,
        image_count=image_count,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


async def _get_image_count(db: AsyncSession, case_id: str) -> int:
    result = await db.execute(
        select(func.count()).where(CaseImage.case_id == case_id)
    )
    return result.scalar_one()


async def _get_image_counts(db: AsyncSession, case_ids: list[str]) -> dict[str, int]:
    """Batch fetch image counts for a list of case IDs in one query."""
    if not case_ids:
        return {}
    result = await db.execute(
        select(CaseImage.case_id, func.count().label("cnt"))
        .where(CaseImage.case_id.in_(case_ids))
        .group_by(CaseImage.case_id)
    )
    return {row.case_id: row.cnt for row in result}


def _assert_access(user: User, case: Case) -> None:
    """
    Raise CaseNotFoundException (not 403) to prevent case enumeration.
    An attacker probing random case IDs gets the same response as a real miss.
    ADMIN always passes — they can read any case.
    """
    if user.role == UserRole.ADMIN:
        return
    if user.role == UserRole.PATIENT and case.patient_id != user.id:
        raise CaseNotFoundException()
    if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
        raise CaseNotFoundException()


# ------------------------------------------------------------------ #
# Create
# ------------------------------------------------------------------ #

async def create_case(
    db: AsyncSession,
    patient: User,
    request: CaseCreateRequest,
) -> CaseResponse:
    """
    Open a new consultation case for the given patient.

    Raises:
        BadRequestException — is_for_self=False without dependent info
    """
    if not request.consent_ai_analysis:
        raise BadRequestException(
            message="CONSENT_REQUIRED: consent_ai_analysis must be true to create a case"
        )

    if not request.is_for_self and request.dependent is None:
        raise BadRequestException(
            message="Dependent information is required when is_for_self=False"
        )

    try:
        c_type = ConsultationType(request.consultation_type)
    except ValueError:
        raise BadRequestException(
            message=f"Invalid consultation_type: {request.consultation_type!r}"
        )

    dep = request.dependent
    now = datetime.now(tz=timezone.utc)
    case = Case(
        id=new_uuid(),
        patient_id=patient.id,
        consultation_type=c_type,
        has_visible_lesion=request.has_visible_lesion,
        is_for_self=request.is_for_self,
        presenting_complaint=request.presenting_complaint,
        consent_ai_analysis=True,
        consent_ai_analysis_at=now,
        consent_research=request.consent_research,
        ai_status=AiStatus.PENDING,
        clinical_status=ClinicalStatus.ACTIVE,
        question_round=0,
        max_question_rounds=5,
        dependent_name=dep.name if dep else None,
        dependent_relationship=dep.relationship if dep else None,
        dependent_dob=dep.date_of_birth if dep else None,
        dependent_gender=dep.gender if dep else None,
    )
    db.add(case)
    await db.flush()

    logger.info("case_created", case_id=case.id, patient_id=patient.id)
    return _to_case_response(case, image_count=0)


# ------------------------------------------------------------------ #
# List
# ------------------------------------------------------------------ #

async def list_cases(
    db: AsyncSession,
    user: User,
    page: int = 1,
    page_size: int = 20,
    clinical_status: str | None = None,
    is_for_self: bool | None = None,
) -> PaginatedCasesResponse:
    """
    Paginated list of cases filtered by the user's role.

    Patient → own cases only.
    Doctor  → assigned cases only.
    Admin   → all cases.
    """
    offset = (page - 1) * page_size

    # Role-based base filter
    if user.role == UserRole.PATIENT:
        base_filter = Case.patient_id == user.id
    elif user.role == UserRole.DOCTOR:
        base_filter = Case.doctor_id == user.id
    else:
        base_filter = None  # Admin: no filter

    filters = [base_filter] if base_filter is not None else []
    if clinical_status:
        try:
            filters.append(Case.clinical_status == ClinicalStatus(clinical_status))
        except ValueError:
            raise BadRequestException(
                message=f"Invalid clinical_status filter: {clinical_status!r}"
            )
    if is_for_self is not None:
        filters.append(Case.is_for_self == is_for_self)

    where_clause = and_(*filters) if filters else True

    count_result = await db.execute(
        select(func.count()).select_from(Case).where(where_clause)
    )
    total = count_result.scalar_one()

    rows_result = await db.execute(
        select(Case)
        .where(where_clause)
        .order_by(Case.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    cases = list(rows_result.scalars().all())

    case_ids = [c.id for c in cases]
    counts = await _get_image_counts(db, case_ids)
    items = [_to_summary_response(c, counts.get(c.id, 0)) for c in cases]

    return PaginatedCasesResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_next=(offset + page_size) < total,
    )


# ------------------------------------------------------------------ #
# Get single
# ------------------------------------------------------------------ #

async def get_case(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> CaseResponse:
    """Return full case detail. Raises CaseNotFoundException if no access."""
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    _assert_access(user, case)
    image_count = await _get_image_count(db, case_id)
    return _to_case_response(case, image_count)


# ------------------------------------------------------------------ #
# Update
# ------------------------------------------------------------------ #

async def update_case(
    db: AsyncSession,
    user: User,
    case_id: str,
    request: CaseUpdateRequest,
) -> CaseResponse:
    """
    Apply PATCH updates to a case.

    Consent: patient-only, immutable once given.
    clinical_status: doctor-only.
    presenting_complaint: patient-only.
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    _assert_access(user, case)

    if request.presenting_complaint is not None:
        if user.role != UserRole.PATIENT:
            raise ForbiddenException(message="Only the patient can update the complaint")
        case.presenting_complaint = request.presenting_complaint

    if request.clinical_status is not None:
        if user.role != UserRole.DOCTOR:
            raise ForbiddenException(
                message="Only a doctor can change the clinical status"
            )
        case.clinical_status = ClinicalStatus(request.clinical_status)

    image_count = await _get_image_count(db, case_id)
    logger.info("case_updated", case_id=case_id, user_id=user.id)
    return _to_case_response(case, image_count)


# ------------------------------------------------------------------ #
# Soft Delete
# ------------------------------------------------------------------ #

async def soft_delete_case(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> None:
    """Mark a case as cancelled. Data is preserved for medical records."""
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException()

    _assert_access(user, case)
    case.ai_status = AiStatus.FAILED
    case.clinical_status = ClinicalStatus.RESOLVED
    logger.info("case_cancelled", case_id=case_id, user_id=user.id)


# ------------------------------------------------------------------ #
# Assign Doctor
# ------------------------------------------------------------------ #

async def assign_doctor(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> CaseResponse:
    """
    Doctor claims a case after QR scan.

    Idempotent: same doctor claiming twice → no-op.
    Different doctor already assigned → 403.
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException()

    if case.doctor_id is not None and case.doctor_id != doctor.id:
        raise ForbiddenException(
            message="This case has already been claimed by another doctor"
        )

    if case.doctor_id != doctor.id:
        case.doctor_id = doctor.id
        logger.info("doctor_assigned", case_id=case_id, doctor_id=doctor.id)

    image_count = await _get_image_count(db, case_id)
    return _to_case_response(case, image_count)


# ------------------------------------------------------------------ #
# Assessment Depth
# ------------------------------------------------------------------ #

_DEPTH_ROUNDS: dict[str, int] = {"quick": 2, "standard": 5, "full": 8}


async def set_assessment_depth(
    db: AsyncSession,
    patient: User,
    case_id: str,
    request: AssessmentDepthRequest,
) -> AssessmentDepthResponse:
    """
    Set the number of Q&A rounds before the case summary is generated.

    Must be called after AI analysis completes (ai_status = completed)
    and before the first question round starts (question_round = 0).

    Raises:
        CaseNotFoundException  — case not found or patient does not own it
        ForbiddenException     — caller is not a patient
        BadRequestException    — AI not yet complete, or questions already started
    """
    if patient.role != UserRole.PATIENT:
        raise ForbiddenException(message="Only the patient can set assessment depth")

    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None or case.patient_id != patient.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before setting assessment depth. "
                    f"Current status: {case.ai_status.value}"
        )

    if case.question_round > 0:
        raise BadRequestException(
            message="Assessment depth cannot be changed once questions have started"
        )

    rounds = _DEPTH_ROUNDS[request.depth]
    case.max_question_rounds = rounds
    await db.flush()

    logger.info(
        "assessment_depth_set",
        case_id=case_id,
        depth=request.depth,
        max_rounds=rounds,
    )
    return AssessmentDepthResponse(
        case_id=case_id,
        depth=request.depth,
        max_question_rounds=rounds,
        message=f"Assessment set to {request.depth} ({rounds} question rounds).",
    )


# ------------------------------------------------------------------ #
# Red Flag Check
# ------------------------------------------------------------------ #

def _build_red_flags_response(case: Case) -> RedFlagsResponse:
    import json
    flags: list[str] = []
    if case.red_flags:
        try:
            flags = json.loads(case.red_flags)
        except (json.JSONDecodeError, TypeError):
            flags = []

    status = case.red_flag_status.value
    if status == RedFlagStatus.NOT_CHECKED.value:
        message = "Red flag check has not been triggered yet."
    elif status == RedFlagStatus.CHECKING.value:
        message = "Red flag check is in progress. Poll this endpoint again shortly."
    elif status == RedFlagStatus.FLAGGED.value:
        message = "Urgent symptoms detected. Please review the advice below."
    else:
        message = "No red flags detected. You may proceed to the case summary."

    return RedFlagsResponse(
        case_id=case.id,
        status=status,
        flags=flags,
        advice=case.red_flag_advice,
        message=message,
    )


async def get_red_flags(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> RedFlagsResponse:
    """
    Return the current red flag check status and results.
    Available to both the patient and assigned doctor.
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    _assert_access(user, case)
    return _build_red_flags_response(case)


async def trigger_red_flag_check(
    db: AsyncSession,
    patient: User,
    case_id: str,
) -> RedFlagsResponse:
    """
    Trigger the systemic / red flag check for a case.

    Runs synchronously using a fast AI prompt against the patient's
    complaint and Q&A answers. Sets red_flag_status to CHECKING while
    the check runs, then updates to CLEAR or FLAGGED.

    Only valid after the conversation is complete (question_round >= max_question_rounds).
    Idempotent — re-triggering a CLEAR or FLAGGED case returns the existing result.
    """
    if patient.role != UserRole.PATIENT:
        raise ForbiddenException(message="Only the patient can trigger the red flag check")

    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None or case.patient_id != patient.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before the red flag check can run"
        )

    if case.question_round < case.max_question_rounds:
        raise BadRequestException(
            message="Complete all question rounds before running the red flag check"
        )

    # Idempotent: already checked
    if case.red_flag_status in (RedFlagStatus.CLEAR, RedFlagStatus.FLAGGED):
        logger.info("red_flag_check_already_done", case_id=case_id, status=case.red_flag_status)
        return _build_red_flags_response(case)

    # Mark as checking and enqueue Celery task
    case.red_flag_status = RedFlagStatus.CHECKING
    await db.flush()

    try:
        from src.workers.tasks.analysis import red_flag_check_task
        red_flag_check_task.delay(case_id)
        logger.info("red_flag_check_triggered", case_id=case_id)
    except Exception as exc:
        logger.error("red_flag_check_enqueue_failed", case_id=case_id, error=str(exc))
        case.red_flag_status = RedFlagStatus.NOT_CHECKED
        await db.flush()
        raise BadRequestException(
            message="Could not enqueue red flag check. Is Redis running?"
        ) from exc

    return _build_red_flags_response(case)


# ------------------------------------------------------------------ #
# Doctor Stats
# ------------------------------------------------------------------ #

async def get_doctor_stats(db: AsyncSession, doctor: User) -> DoctorStatsResponse:
    """Aggregate stats for the doctor home screen dashboard."""
    today_start = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    async def _count(where_clause) -> int:
        res = await db.execute(
            select(func.count()).select_from(Case).where(where_clause)
        )
        return res.scalar_one()

    d = Case.doctor_id == doctor.id

    today_cases = await _count(and_(d, Case.created_at >= today_start))
    pending_review = await _count(and_(d, Case.ai_status == AiStatus.COMPLETED))
    in_progress = await _count(and_(d, Case.ai_status == AiStatus.PROCESSING))
    completed_today = await _count(
        and_(d, Case.clinical_status == ClinicalStatus.RESOLVED,
             Case.updated_at >= today_start)
    )
    total_assigned = await _count(d)

    return DoctorStatsResponse(
        today_cases=today_cases,
        pending_review=pending_review,
        in_progress=in_progress,
        completed_today=completed_today,
        total_assigned=total_assigned,
    )
