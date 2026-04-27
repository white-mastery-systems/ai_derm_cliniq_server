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

from datetime import date, datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from src.cases.schemas import (
    AdjacentVisitsResponse,
    AssessmentDepthRequest,
    AssessmentDepthResponse,
    BookmarkResponse,
    CaseCreateRequest,
    CaseResponse,
    CaseSearchItem,
    CaseSummaryResponse,
    CaseUpdateRequest,
    ClinicalFeaturesRequest,
    ClinicalFeaturesResponse,
    ComplaintsResponse,
    DoctorCaseCreateRequest,
    DoctorStatsResponse,
    PaginatedCasesResponse,
    PaginatedSearchResponse,
    RedFlagsResponse,
    VisualFindingsGenerateResponse,
    VisualFindingsPatchRequest,
    VisualFindingsResponse,
)
from src.exceptions import (
    AIServiceException,
    BadRequestException,
    CaseNotFoundException,
    ForbiddenException,
)
from src.images.schemas import ImageResponse
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import AiStatus, Case, ClinicalStatus, ConsultationType, RedFlagStatus
from src.models.case_image import CaseImage
from src.models.doctor_profile import DoctorProfile
from src.models.patient_profile import PatientProfile
from src.models.user import User, UserRole
from src.storage import gcs

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Internal helpers
# ------------------------------------------------------------------ #

def _parse_symptom_tags(raw: str | None) -> list[str]:
    """Deserialise the JSON symptom_tags stored on Case into a list."""
    if not raw:
        return []
    import json as _json
    try:
        parsed = _json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _build_image_response(img: CaseImage) -> ImageResponse:
    """Convert a CaseImage row to an ImageResponse with a fresh signed URL."""
    try:
        signed_url = gcs.get_signed_url(img.gcs_path)
    except Exception:
        signed_url = None
    return ImageResponse(
        id=img.id,
        case_id=img.case_id,
        image_type=img.image_type.value,
        original_filename=img.original_filename,
        mime_type=img.mime_type,
        size_bytes=img.size_bytes,
        upload_order=img.upload_order,
        signed_url=signed_url,
        created_at=img.created_at,
    )


def _to_case_response(
    case: Case,
    image_count: int = 0,
    images: list[ImageResponse] | None = None,
    patient_name: str | None = None,
    patient_age: int | None = None,
    patient_gender: str | None = None,
    patient_avatar_url: str | None = None,
    doctor_name: str | None = None,
    doctor_specialization: str | None = None,
    doctor_clinic_name: str | None = None,
    doctor_avatar_url: str | None = None,
    visit_index: int | None = None,
    total_visits: int | None = None,
) -> CaseResponse:
    return CaseResponse(
        id=case.id,
        case_number=case.case_number,
        display_id=f"AI-{case.case_number}" if case.case_number else None,
        case_title=case.case_title,
        original_case_id=case.original_case_id,
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
        body_location=case.body_location,
        symptom_progression=case.symptom_progression,
        symptom_tags=_parse_symptom_tags(case.symptom_tags),
        is_bookmarked=case.is_bookmarked,
        presenting_complaint=case.presenting_complaint,
        case_summary=case.case_summary,
        celery_task_id=case.celery_task_id,
        question_round=case.question_round,
        max_question_rounds=case.max_question_rounds,
        image_count=image_count,
        images=images or [],
        patient_name=patient_name,
        patient_age=patient_age,
        patient_gender=patient_gender,
        patient_avatar_url=patient_avatar_url,
        doctor_name=doctor_name,
        doctor_specialization=doctor_specialization,
        doctor_clinic_name=doctor_clinic_name,
        doctor_avatar_url=doctor_avatar_url,
        visit_index=visit_index,
        total_visits=total_visits,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


def _to_summary_response(
    case: Case,
    image_count: int = 0,
    patient_name: str | None = None,
    patient_avatar_url: str | None = None,
    doctor_name: str | None = None,
    doctor_specialization: str | None = None,
    doctor_clinic_name: str | None = None,
    doctor_avatar_url: str | None = None,
) -> CaseSummaryResponse:
    return CaseSummaryResponse(
        id=case.id,
        case_number=case.case_number,
        display_id=f"AI-{case.case_number}" if case.case_number else None,
        case_title=case.case_title,
        consultation_type=case.consultation_type.value,
        ai_status=case.ai_status.value,
        clinical_status=case.clinical_status.value,
        is_for_self=case.is_for_self,
        dependent_name=case.dependent_name,
        dependent_relationship=case.dependent_relationship,
        consent_ai_analysis=case.consent_ai_analysis,
        has_visible_lesion=case.has_visible_lesion,
        body_location=case.body_location,
        symptom_progression=case.symptom_progression,
        symptom_tags=_parse_symptom_tags(case.symptom_tags),
        case_summary=case.case_summary,
        is_bookmarked=case.is_bookmarked,
        image_count=image_count,
        patient_name=patient_name,
        patient_avatar_url=patient_avatar_url,
        doctor_name=doctor_name,
        doctor_specialization=doctor_specialization,
        doctor_clinic_name=doctor_clinic_name,
        doctor_avatar_url=doctor_avatar_url,
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

    # Require complete profile before starting a checkup (is_for_self only)
    # Dependent cases carry their own DOB/gender in the request
    if request.is_for_self:
        profile_result = await db.execute(
            select(PatientProfile).where(PatientProfile.user_id == patient.id)
        )
        profile = profile_result.scalar_one_or_none()
        if not profile or not profile.date_of_birth or not profile.gender:
            raise BadRequestException(
                message="Please complete your profile (date of birth and gender) "
                        "before starting a checkup. Update via PATCH /api/v1/users/me"
            )

    if not request.is_for_self and request.dependent is None and request.dependent_id is None:
        raise BadRequestException(
            message="Dependent information is required when is_for_self=False. "
                    "Provide either dependent_id (saved) or dependent (inline)."
        )

    # Validate original_case_id belongs to this patient
    if request.original_case_id:
        orig_check = await db.execute(
            select(Case.id, Case.patient_id).where(Case.id == request.original_case_id)
        )
        orig_row = orig_check.one_or_none()
        if orig_row is None or orig_row.patient_id != patient.id:
            raise BadRequestException(
                message="original_case_id not found or does not belong to this patient"
            )

    try:
        c_type = ConsultationType(request.consultation_type)
    except ValueError:
        raise BadRequestException(
            message=f"Invalid consultation_type: {request.consultation_type!r}"
        )

    # Resolve dependent data — saved profile (dependent_id) takes priority over inline
    dep = request.dependent
    resolved_dependent_id: str | None = None

    if request.dependent_id:
        from src.models.dependent import Dependent as _Dependent
        dep_result = await db.execute(
            select(_Dependent).where(_Dependent.id == request.dependent_id)
        )
        saved_dep = dep_result.scalar_one_or_none()
        if saved_dep is None or saved_dep.patient_id != patient.id:
            raise BadRequestException(
                message=f"No saved dependent found with id: {request.dependent_id}"
            )
        resolved_dependent_id = saved_dep.id
        dep_name = saved_dep.name
        dep_dob = saved_dep.date_of_birth
        dep_gender = saved_dep.gender
    elif dep:
        dep_name = dep.name
        dep_dob = dep.date_of_birth
        dep_gender = dep.gender
    else:
        dep_name = dep_dob = dep_gender = None

    now = datetime.now(tz=timezone.utc)
    case = Case(
        id=new_uuid(),
        patient_id=patient.id,
        original_case_id=request.original_case_id,
        consultation_type=c_type,
        has_visible_lesion=request.has_visible_lesion,
        is_for_self=request.is_for_self,
        body_location=request.body_location,
        presenting_complaint=request.presenting_complaint,
        symptom_progression=request.symptom_progression,
        consent_ai_analysis=True,
        consent_ai_analysis_at=now,
        consent_research=request.consent_research,
        ai_status=AiStatus.PENDING,
        clinical_status=ClinicalStatus.ACTIVE,
        question_round=0,
        max_question_rounds=5,
        dependent_id=resolved_dependent_id,
        dependent_name=dep_name,
        dependent_dob=dep_dob,
        dependent_gender=dep_gender,
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
    bookmarked: bool | None = None,
) -> PaginatedCasesResponse:
    """
    Paginated list of cases filtered by the user's role.

    Patient → own cases only.
    Doctor  → assigned cases only.
    Admin   → all cases.

    bookmarked=true  → only bookmarked cases (Important Cases list, doctor-facing).
    """
    offset = (page - 1) * page_size

    # Role-based base filter
    if user.role == UserRole.PATIENT:
        base_filter = Case.patient_id == user.id
    elif user.role == UserRole.DOCTOR:
        base_filter = Case.doctor_id == user.id
    else:
        base_filter = None  # Admin: no filter

    filters = [Case.is_deleted == False]  # noqa: E712  # always exclude soft-deleted cases
    if base_filter is not None:
        filters.append(base_filter)
    if clinical_status:
        try:
            filters.append(Case.clinical_status == ClinicalStatus(clinical_status))
        except ValueError:
            raise BadRequestException(
                message=f"Invalid clinical_status filter: {clinical_status!r}"
            )
    if is_for_self is not None:
        filters.append(Case.is_for_self == is_for_self)
    if bookmarked is not None:
        filters.append(Case.is_bookmarked == bookmarked)  # noqa: E712

    where_clause = and_(*filters) if filters else True

    count_result = await db.execute(
        select(func.count()).select_from(Case).where(where_clause)
    )
    total = count_result.scalar_one()

    DoctorUser = aliased(User)
    rows_result = await db.execute(
        select(
            Case,
            User.full_name.label("patient_name"),
            PatientProfile.avatar_url.label("patient_avatar_url"),
            DoctorUser.full_name.label("doctor_name"),
            DoctorProfile.specialization.label("doctor_specialization"),
            DoctorProfile.clinic_name.label("doctor_clinic_name"),
            DoctorProfile.avatar_url.label("doctor_avatar_url"),
        )
        .join(User, User.id == Case.patient_id)
        .outerjoin(PatientProfile, PatientProfile.user_id == Case.patient_id)
        .outerjoin(DoctorUser, DoctorUser.id == Case.doctor_id)
        .outerjoin(DoctorProfile, DoctorProfile.user_id == Case.doctor_id)
        .where(where_clause)
        .order_by(Case.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = rows_result.all()

    case_ids = [row.Case.id for row in rows]
    counts = await _get_image_counts(db, case_ids)
    items = [
        _to_summary_response(
            row.Case,
            counts.get(row.Case.id, 0),
            patient_name=row.patient_name,
            patient_avatar_url=row.patient_avatar_url,
            doctor_name=row.doctor_name,
            doctor_specialization=row.doctor_specialization,
            doctor_clinic_name=row.doctor_clinic_name,
            doctor_avatar_url=row.doctor_avatar_url,
        )
        for row in rows
    ]

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

    # ---- Patient demographics ----
    patient_name: str | None = None
    patient_age: int | None = None
    patient_gender: str | None = None
    patient_avatar_url: str | None = None

    patient_result = await db.execute(select(User).where(User.id == case.patient_id))
    patient_user = patient_result.scalar_one_or_none()
    if patient_user:
        patient_name = patient_user.full_name
        profile_result = await db.execute(
            select(PatientProfile).where(PatientProfile.user_id == case.patient_id)
        )
        profile = profile_result.scalar_one_or_none()
        if profile:
            patient_gender = profile.gender
            patient_avatar_url = profile.avatar_url
            if profile.date_of_birth:
                today = date.today()
                dob = profile.date_of_birth
                patient_age = today.year - dob.year - (
                    (today.month, today.day) < (dob.month, dob.day)
                )

    # ---- Doctor details ----
    doctor_name: str | None = None
    doctor_specialization: str | None = None
    doctor_clinic_name: str | None = None
    doctor_avatar_url: str | None = None

    if case.doctor_id:
        doctor_result = await db.execute(select(User).where(User.id == case.doctor_id))
        doctor_user = doctor_result.scalar_one_or_none()
        if doctor_user:
            doctor_name = doctor_user.full_name
            dr_profile_result = await db.execute(
                select(DoctorProfile).where(DoctorProfile.user_id == case.doctor_id)
            )
            dr_profile = dr_profile_result.scalar_one_or_none()
            if dr_profile:
                doctor_specialization = dr_profile.specialization
                doctor_clinic_name = dr_profile.clinic_name
                doctor_avatar_url = dr_profile.avatar_url

    # ---- Visit X of Y ----
    # Total cases for this patient
    total_visits_result = await db.execute(
        select(func.count()).select_from(Case).where(Case.patient_id == case.patient_id)
    )
    total_visits = total_visits_result.scalar_one()

    # This case's chronological position (1-based)
    visit_index_result = await db.execute(
        select(func.count()).select_from(Case).where(
            and_(
                Case.patient_id == case.patient_id,
                Case.created_at <= case.created_at,
            )
        )
    )
    visit_index = visit_index_result.scalar_one()

    # ---- All images (initial + mid-consultation) ----
    images_result = await db.execute(
        select(CaseImage)
        .where(CaseImage.case_id == case_id)
        .order_by(CaseImage.upload_order)
    )
    images = [_build_image_response(img) for img in images_result.scalars().all()]

    return _to_case_response(
        case, image_count, images,
        patient_name, patient_age, patient_gender, patient_avatar_url,
        doctor_name, doctor_specialization, doctor_clinic_name, doctor_avatar_url,
        visit_index, total_visits,
    )


# ------------------------------------------------------------------ #
# Adjacent Visits
# ------------------------------------------------------------------ #

async def get_adjacent_visits(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> AdjacentVisitsResponse:
    """
    Return the prev/next case_id for the ← → navigation arrows on the
    Case Report screen.

    Visits are ordered chronologically (created_at ASC).
    - prev_case_id: the visit immediately before this one (None if first)
    - next_case_id: the visit immediately after this one (None if latest)

    Access rules mirror get_case() — patient sees own cases, doctor sees
    assigned cases, admin sees all.
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    _assert_access(user, case)

    # Resolve the root case ID for this condition thread.
    # A follow-up stores original_case_id pointing to the case it follows.
    # We walk up one level to find the root (new_complaint with no original_case_id).
    root_id = case.original_case_id or case.id
    if root_id != case.id:
        root_check = await db.execute(
            select(Case.original_case_id).where(Case.id == root_id)
        )
        root_row = root_check.one_or_none()
        if root_row and root_row.original_case_id:
            root_id = root_row.original_case_id

    # All cases in this condition thread: the root itself + all follow-ups of it
    thread_result = await db.execute(
        select(Case.id, Case.created_at)
        .where(or_(Case.id == root_id, Case.original_case_id == root_id))
        .order_by(Case.created_at.asc())
    )
    ids = [row.id for row in thread_result.all()]

    # If case_id somehow isn't in the thread (shouldn't happen), fall back to it alone
    if case_id not in ids:
        ids = [case_id]

    try:
        idx = ids.index(case_id)
    except ValueError:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    return AdjacentVisitsResponse(
        prev_case_id=ids[idx - 1] if idx > 0 else None,
        next_case_id=ids[idx + 1] if idx < len(ids) - 1 else None,
        visit_index=idx + 1,
        total_visits=len(ids),
    )


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
        is_assigned_doctor = (user.role == UserRole.DOCTOR and case.doctor_id == user.id)
        if user.role != UserRole.PATIENT and not is_assigned_doctor:
            raise ForbiddenException(message="Only the patient or assigned doctor can update the complaint")
        if isinstance(request.presenting_complaint, list):
            case.presenting_complaint = ". ".join(
                c.strip() for c in request.presenting_complaint if c.strip()
            )
        else:
            case.presenting_complaint = request.presenting_complaint

    if request.body_location is not None:
        case.body_location = request.body_location

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
    case.is_deleted = True
    logger.info("case_soft_deleted", case_id=case_id, user_id=user.id)


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

async def set_assessment_depth(
    db: AsyncSession,
    user: User,
    case_id: str,
    request: AssessmentDepthRequest,
) -> AssessmentDepthResponse:
    """
    Set the number of Q&A rounds before the case summary is generated.

    Can be called any time before the first question round starts (question_round = 0).
    Works for both patient flow (after AI completes) and doctor flow (before AI is triggered).

    Raises:
        CaseNotFoundException  — case not found or caller does not have access
        BadRequestException    — questions already started
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    _assert_access(user, case)

    if case.question_round > 0:
        raise BadRequestException(
            message="Assessment depth cannot be changed once questions have started"
        )

    case.max_question_rounds = request.rounds
    await db.flush()

    logger.info(
        "assessment_depth_set",
        case_id=case_id,
        rounds=request.rounds,
    )
    return AssessmentDepthResponse(
        case_id=case_id,
        rounds=request.rounds,
        max_question_rounds=request.rounds,
        message=f"Assessment set to {request.rounds} question rounds.",
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
    user: User,
    case_id: str,
    selected_symptoms: list[str] | None = None,
) -> RedFlagsResponse:
    """
    Trigger the systemic / red flag check for a case.

    Runs synchronously using a fast AI prompt against the patient's
    complaint and Q&A answers. Sets red_flag_status to CHECKING while
    the check runs, then updates to CLEAR or FLAGGED.

    Only valid after the conversation is complete (question_round >= max_question_rounds).
    Idempotent — re-triggering a CLEAR or FLAGGED case returns the existing result.
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    _assert_access(user, case)

    if case.ai_status != AiStatus.COMPLETED:
        raise BadRequestException(
            message="AI analysis must complete before the red flag check can run"
        )

    # Allow if rounds exhausted OR if conversation was finished early (case_summary is set)
    if case.question_round < case.max_question_rounds and not case.case_summary:
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
        red_flag_check_task.delay(case_id, selected_symptoms or [])
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
# Complaint Suggestions
# ------------------------------------------------------------------ #

async def get_complaint_suggestions(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> ComplaintsResponse:
    """
    Return AI-generated complaint options for the Presenting Complaint screen.

    Visible-lesion flow (has_visible_lesion=True):
      - Requires at least one image to be uploaded first.
      - Sends images + patient particulars to Gemini → image-contextual complaints.

    No-lesion flow (has_visible_lesion=False):
      - Text-only call using patient age/sex → general subjective complaints.

    The Flutter app shows these as checkboxes. The patient selects items and
    optionally adds free text, then submits via PATCH /cases/{id} as
    presenting_complaint (comma-joined selected labels + custom text).
    """
    import asyncio
    from datetime import date as _date

    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    _assert_access(user, case)

    # Build age / sex from the patient's profile (not the caller's — caller may be doctor)
    profile_result = await db.execute(
        select(PatientProfile).where(PatientProfile.user_id == case.patient_id)
    )
    profile = profile_result.scalar_one_or_none()

    age, sex = "unknown", "unknown"
    if profile:
        if profile.date_of_birth:
            age = str((_date.today() - profile.date_of_birth).days // 365)
        if profile.gender:
            sex = profile.gender

    from src.ai.gemini_client import call_gemini, extract_json
    from src.ai.prompts.image_analysis_prompts import ImageAnalysisPrompts
    from src.ai.prompts.patient_consultation_prompts import PatientConsultationPrompts

    if case.has_visible_lesion:
        # Require at least one uploaded image before generating image-based complaints
        images_result = await db.execute(
            select(CaseImage)
            .where(CaseImage.case_id == case_id)
            .order_by(CaseImage.upload_order)
        )
        images = list(images_result.scalars().all())

        if not images:
            raise BadRequestException(
                message="Upload at least one photo before loading complaint suggestions"
            )

        from src.storage import gcs

        image_bytes: list[bytes] = []
        for img in images:
            try:
                image_bytes.append(gcs.download_bytes(img.gcs_path))
            except Exception as exc:
                raise AIServiceException(
                    message=f"Could not load image for complaint suggestions: {exc}"
                ) from exc

        prompt = ImageAnalysisPrompts.get_complaints_from_image().format(
            personal_particulars=f"Age: {age}, Sex: {sex}"
        )

        try:
            response_text = await asyncio.to_thread(call_gemini, prompt, image_bytes)
            data = await asyncio.to_thread(extract_json, response_text)
        except Exception as exc:
            raise AIServiceException(
                message=f"Could not generate complaint suggestions: {exc}"
            ) from exc

        source = "image"

    else:
        prompt = PatientConsultationPrompts.get_general_complaints().format(
            age=age, sex=sex
        )

        try:
            response_text = await asyncio.to_thread(call_gemini, prompt)
            data = await asyncio.to_thread(extract_json, response_text)
        except Exception as exc:
            raise AIServiceException(
                message=f"Could not generate complaint suggestions: {exc}"
            ) from exc

        source = "general"

    complaints = data.get("Complaint", [])
    return ComplaintsResponse(
        case_id=case_id,
        complaints=complaints if isinstance(complaints, list) else [],
        source=source,
    )


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


# ------------------------------------------------------------------ #
# Doctor-side Case Creation
# ------------------------------------------------------------------ #

async def create_case_by_doctor(
    db: AsyncSession,
    doctor: User,
    request: DoctorCaseCreateRequest,
) -> CaseResponse:
    """
    Doctor creates a case on behalf of a patient identified by name + email.

    FIND-OR-CREATE:
    - Email found → use that patient; fill in missing DOB/gender if provided.
    - Email not found → create new patient account (no password set).
      The patient claims the account later via forgot-password OTP.

    Doctor is immediately assigned (doctor_id = doctor.id).
    Consent is implied by the clinical encounter.
    """
    from src.auth.security import generate_patient_code

    # Resolve effective DOB: explicit date wins; otherwise approximate from age
    effective_dob = request.patient_date_of_birth
    if effective_dob is None and request.patient_age is not None:
        from datetime import timedelta
        effective_dob = (datetime.now(tz=timezone.utc).date()
                         - timedelta(days=request.patient_age * 365))

    # ── 1. Find or create patient ─────────────────────────────────── #
    patient_result = await db.execute(
        select(User).where(User.email == request.patient_email)
    )
    patient = patient_result.scalar_one_or_none()

    if patient is not None:
        # Existing account — must be an active patient
        if not patient.is_active or patient.role != UserRole.PATIENT:
            raise BadRequestException(
                message=f"An account with email {request.patient_email!r} exists "
                        "but is not an active patient account"
            )
        # Fill in missing profile fields if doctor provided them
        if request.patient_date_of_birth or request.patient_gender:
            profile_result = await db.execute(
                select(PatientProfile).where(PatientProfile.user_id == patient.id)
            )
            profile = profile_result.scalar_one_or_none()
            if profile:
                if not profile.date_of_birth and effective_dob:
                    profile.date_of_birth = effective_dob
                if not profile.gender and request.patient_gender:
                    profile.gender = request.patient_gender
        logger.info("doctor_case_patient_found", patient_id=patient.id, doctor_id=doctor.id)

    else:
        # New patient — create account without a password
        # Patient claims account later via forgot-password OTP
        patient = User(
            email=request.patient_email,
            full_name=request.patient_name,
            role=UserRole.PATIENT,
            password_hash=None,
            is_active=True,
            is_verified=False,
        )
        db.add(patient)
        await db.flush()  # assigns patient.id

        # Generate unique patient_code
        for _ in range(10):
            code = generate_patient_code()
            existing_code = await db.execute(
                select(PatientProfile).where(PatientProfile.patient_code == code)
            )
            if existing_code.scalar_one_or_none() is None:
                break
        else:
            raise RuntimeError("Failed to generate a unique patient code")

        profile = PatientProfile(
            user_id=patient.id,
            patient_code=code,
            date_of_birth=effective_dob,
            gender=request.patient_gender,
        )
        db.add(profile)
        await db.flush()
        logger.info(
            "doctor_case_patient_created",
            patient_id=patient.id,
            email=request.patient_email,
            doctor_id=doctor.id,
        )

    try:
        c_type = ConsultationType(request.consultation_type)
    except ValueError:
        raise BadRequestException(
            message=f"Invalid consultation_type: {request.consultation_type!r}"
        )

    # Validate original_case_id belongs to the patient
    if request.original_case_id:
        orig_check = await db.execute(
            select(Case.id, Case.patient_id).where(Case.id == request.original_case_id)
        )
        orig_row = orig_check.one_or_none()
        if orig_row is None or orig_row.patient_id != patient.id:
            raise BadRequestException(
                message="original_case_id not found or does not belong to this patient"
            )

    now = datetime.now(tz=timezone.utc)
    case = Case(
        id=new_uuid(),
        patient_id=patient.id,
        doctor_id=doctor.id,
        consultation_type=c_type,
        has_visible_lesion=request.has_visible_lesion,
        is_for_self=True,
        body_location=request.body_location,
        presenting_complaint=request.presenting_complaint,
        original_case_id=request.original_case_id,
        symptom_progression=request.symptom_progression,
        consent_ai_analysis=True,
        consent_ai_analysis_at=now,
        consent_research=request.consent_research,
        ai_status=AiStatus.PENDING,
        clinical_status=ClinicalStatus.ACTIVE,
        question_round=0,
        max_question_rounds=5,
    )
    db.add(case)
    await db.flush()

    logger.info(
        "case_created_by_doctor",
        case_id=case.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
    )
    return _to_case_response(case, image_count=0)


# ------------------------------------------------------------------ #
# Search
# ------------------------------------------------------------------ #

async def search_cases(
    db: AsyncSession,
    user: User,
    q: str,
    page: int = 1,
    page_size: int = 20,
) -> PaginatedSearchResponse:
    """
    Search cases by patient full name or case ID (partial, case-insensitive).

    Doctor → searches only their assigned cases.
    Admin  → searches all cases.

    Returns CaseSearchItem rows that include patient_name for display.
    """
    offset = (page - 1) * page_size
    pattern = f"%{q}%"

    # Join Case → User (patient) + PatientProfile for name and avatar
    base_query = (
        select(
            Case,
            User.full_name.label("patient_name"),
            PatientProfile.avatar_url.label("patient_avatar_url"),
        )
        .join(User, User.id == Case.patient_id)
        .outerjoin(PatientProfile, PatientProfile.user_id == Case.patient_id)
        .where(
            or_(
                Case.id.ilike(pattern),
                User.full_name.ilike(pattern),
            )
        )
    )

    if user.role == UserRole.DOCTOR:
        base_query = base_query.where(Case.doctor_id == user.id)

    count_query = select(func.count()).select_from(base_query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    rows_result = await db.execute(
        base_query
        .order_by(Case.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = rows_result.all()

    case_ids = [row.Case.id for row in rows]
    counts = await _get_image_counts(db, case_ids)

    items = [
        CaseSearchItem(
            id=row.Case.id,
            case_number=row.Case.case_number,
            display_id=f"AI-{row.Case.case_number}" if row.Case.case_number else None,
            patient_id=row.Case.patient_id,
            patient_name=row.patient_name,
            consultation_type=row.Case.consultation_type.value,
            ai_status=row.Case.ai_status.value,
            clinical_status=row.Case.clinical_status.value,
            body_location=row.Case.body_location,
            presenting_complaint=row.Case.presenting_complaint,
            image_count=counts.get(row.Case.id, 0),
            created_at=row.Case.created_at,
        )
        for row in rows
    ]

    return PaginatedSearchResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_next=(offset + page_size) < total,
    )


# ------------------------------------------------------------------ #
# Doctor Diagnose Flow — Visual Findings
# ------------------------------------------------------------------ #

async def generate_visual_findings(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> VisualFindingsGenerateResponse:
    """
    Enqueue the Celery task that runs 3-image AI analysis (clinical →
    dermoscopy → pathology) and stores the result in Case.visual_findings.

    Rules:
    - Doctor must be assigned to this case.
    - At least one image of type clinical, dermoscopy, or pathology must exist.
    - Idempotent: if visual_findings already populated, returns immediately.
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    _assert_access(doctor, case)

    if case.visual_findings:
        return VisualFindingsGenerateResponse(
            case_id=case_id,
            task_id=case.celery_task_id or "",
            message="Visual findings already generated. Use GET /visual-findings to read them.",
        )

    # Require at least one doctor-flow image type to be uploaded
    from src.models.case_image import ImageType
    image_check = await db.execute(
        select(func.count()).where(
            CaseImage.case_id == case_id,
            CaseImage.image_type.in_([
                ImageType.CLINICAL, ImageType.DERMOSCOPY, ImageType.PATHOLOGY
            ]),
        )
    )
    if image_check.scalar_one() == 0:
        raise BadRequestException(
            message="Upload at least one clinical, dermoscopy, or pathology image "
                    "before generating visual findings."
        )

    try:
        from src.workers.tasks.doctor_analysis import generate_visual_findings_task
        task = generate_visual_findings_task.delay(case_id)
        case.celery_task_id = task.id
        case.ai_status = AiStatus.PROCESSING
        await db.flush()
        logger.info("visual_findings_enqueued", case_id=case_id, task_id=task.id)
    except Exception as exc:
        raise BadRequestException(
            message="Could not enqueue visual findings task. Is Redis running?"
        ) from exc

    return VisualFindingsGenerateResponse(
        case_id=case_id,
        task_id=task.id,
        message="Visual findings generation started. Poll GET /ai/status for progress.",
    )


async def get_visual_findings(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> VisualFindingsResponse:
    """Return the stored visual findings JSON for a case."""
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    _assert_access(doctor, case)

    findings: dict = {}
    if case.visual_findings:
        import json as _json
        try:
            findings = _json.loads(case.visual_findings)
        except (ValueError, TypeError):
            findings = {}

    return VisualFindingsResponse(
        case_id=case_id,
        ai_status=case.ai_status.value,
        clinical=findings.get("clinical", {}),
        dermoscopy=findings.get("dermoscopy", {}),
        pathology=findings.get("pathology", {}),
    )


async def patch_visual_findings(
    db: AsyncSession,
    doctor: User,
    case_id: str,
    request: VisualFindingsPatchRequest,
) -> VisualFindingsResponse:
    """
    Doctor edits the overall description text for clinical or dermoscopy findings.
    AI reconcile prompt updates the structured fields to stay consistent.
    """
    import asyncio as _asyncio
    import json as _json

    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    _assert_access(doctor, case)

    if not case.visual_findings:
        raise BadRequestException(
            message="Visual findings have not been generated yet for this case."
        )

    findings: dict = {}
    try:
        findings = _json.loads(case.visual_findings)
    except (ValueError, TypeError):
        findings = {}

    # Fetch personal particulars for reconcile context
    profile_result = await db.execute(
        select(PatientProfile).where(PatientProfile.user_id == case.patient_id)
    )
    profile = profile_result.scalar_one_or_none()
    parts = []
    if profile:
        if profile.date_of_birth:
            from datetime import date as _date
            age = (_date.today() - profile.date_of_birth).days // 365
            parts.append(f"Age: {age}")
        if profile.gender:
            parts.append(f"Sex: {profile.gender}")
    personal_particulars = ", ".join(parts) if parts else "Age: unknown, Sex: unknown"

    from src.ai.gemini_client import call_gemini, extract_json
    from src.ai.prompts.doctor_image_prompts import DoctorImageAnalysisPrompts

    # ── Reconcile clinical section ──────────────────────────────── #
    if request.clinical_overall_description is not None:
        clinical_data = findings.get("clinical", {})
        clinical_data["overall_description"] = request.clinical_overall_description
        prompt = DoctorImageAnalysisPrompts.reconcile_clinical().format(
            overall_description=request.clinical_overall_description,
            structured_data_json=_json.dumps(clinical_data),
            personal_particulars=personal_particulars,
        )
        try:
            raw = await _asyncio.to_thread(call_gemini, prompt)
            reconciled = await _asyncio.to_thread(extract_json, raw)
            reconciled["overall_description"] = request.clinical_overall_description
            findings["clinical"] = reconciled
        except Exception as exc:
            logger.warning("reconcile_clinical_failed", case_id=case_id, error=str(exc))
            findings["clinical"] = clinical_data

    # ── Reconcile dermoscopy section ────────────────────────────── #
    if request.dermoscopy_overall_description is not None:
        dermoscopy_data = findings.get("dermoscopy", {})
        dermoscopy_data["overall_dermoscopic_summary"] = request.dermoscopy_overall_description
        prompt = DoctorImageAnalysisPrompts.reconcile_dermoscopic().format(
            overall_description=request.dermoscopy_overall_description,
            structured_data_json=_json.dumps(dermoscopy_data),
            personal_particulars=personal_particulars,
        )
        try:
            raw = await _asyncio.to_thread(call_gemini, prompt)
            reconciled = await _asyncio.to_thread(extract_json, raw)
            reconciled["overall_dermoscopic_summary"] = request.dermoscopy_overall_description
            findings["dermoscopy"] = reconciled
        except Exception as exc:
            logger.warning("reconcile_dermoscopy_failed", case_id=case_id, error=str(exc))
            findings["dermoscopy"] = dermoscopy_data

    case.visual_findings = _json.dumps(findings)
    await db.flush()
    logger.info("visual_findings_patched", case_id=case_id)

    return VisualFindingsResponse(
        case_id=case_id,
        ai_status=case.ai_status.value,
        clinical=findings.get("clinical", {}),
        dermoscopy=findings.get("dermoscopy", {}),
        pathology=findings.get("pathology", {}),
    )


async def save_clinical_features(
    db: AsyncSession,
    doctor: User,
    case_id: str,
    request: ClinicalFeaturesRequest,
) -> ClinicalFeaturesResponse:
    """
    Save the doctor's confirmed clinical feature checklist.
    Stored in DoctorReview.clinical_indicators (creates the review row if needed).
    additional_observations stored in DoctorReview.review_notes.
    """
    import json as _json
    from src.models.doctor_review import DoctorReview

    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    _assert_access(doctor, case)

    review_result = await db.execute(
        select(DoctorReview).where(DoctorReview.case_id == case_id)
    )
    review = review_result.scalar_one_or_none()

    features_json = _json.dumps(request.features)

    if review is None:
        from src.models.base import new_uuid
        review = DoctorReview(
            id=new_uuid(),
            case_id=case_id,
            doctor_id=doctor.id,
            clinical_indicators=features_json,
            review_notes=request.additional_observations,
            review_status="in_progress",
        )
        db.add(review)
    else:
        review.clinical_indicators = features_json
        if request.additional_observations is not None:
            review.review_notes = request.additional_observations

    await db.flush()
    logger.info("clinical_features_saved", case_id=case_id, count=len(request.features))

    return ClinicalFeaturesResponse(
        case_id=case_id,
        features=request.features,
        additional_observations=request.additional_observations,
        message=f"Saved {len(request.features)} clinical features.",
    )


# ------------------------------------------------------------------ #
# Bookmark
# ------------------------------------------------------------------ #

async def toggle_bookmark(
    db: AsyncSession,
    doctor: User,
    case_id: str,
) -> BookmarkResponse:
    """
    Toggle the bookmark flag on a case.

    Only the doctor assigned to the case can bookmark it.
    Calling this endpoint twice returns the case to its previous state (toggle).

    Returns the new is_bookmarked state.
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()

    if case is None or case.doctor_id != doctor.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    case.is_bookmarked = not case.is_bookmarked
    await db.flush()

    logger.info(
        "case_bookmark_toggled",
        case_id=case_id,
        doctor_id=doctor.id,
        is_bookmarked=case.is_bookmarked,
    )
    return BookmarkResponse(case_id=case_id, is_bookmarked=case.is_bookmarked)
