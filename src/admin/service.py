"""
admin/service.py — Admin Dashboard Business Logic
==================================================

Five operations:
1. list_users   — paginated user list with optional role/is_active filter
2. get_user     — single user detail with role-specific profile
3. update_user  — change is_active, is_verified, role
4. list_cases   — paginated case list across all patients, with filters
5. get_stats    — platform-wide aggregated counts

SECURITY NOTES
--------------
All functions in this module are called only from admin endpoints,
which are guarded by the `require_admin` dependency. The service
layer trusts that guard and does not re-check the caller's role.

WRITE SCOPE
-----------
Admins write only to the User table (account state). Clinical tables
(Case, DoctorReview, CaseReport, etc.) are read-only from admin.

ROLE CHANGE GUARD
-----------------
Changing a user's role is allowed but carries clinical risk:
a doctor mid-review becoming a patient would break case ownership.
The endpoint allows it but logs clearly. Consider adding a business
rule in production to block role changes on active cases.

PAGINATION
----------
Consistent with GET /cases in cases/service.py:
  page=1, page_size=20 default
  offset = (page - 1) * page_size
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.admin.schemas import (
    AdminCaseItem,
    AdminDoctorProfileOut,
    AdminPatientProfileOut,
    AdminUserDetail,
    AdminUserItem,
    AdminStatsResponse,
    AiSettingsResponse,
    PaginatedAdminCasesResponse,
    PaginatedAdminUsersResponse,
    UpdateAiSettingsRequest,
)
from src.exceptions import BadRequestException, UserNotFoundException
from src.logger import get_logger
from src.models.case import AiStatus, Case
from src.models.case_report import CaseReport
from src.models.user import User, UserRole

logger = get_logger(__name__)


# ================================================================== #
# User Management
# ================================================================== #

async def list_users(
    db: AsyncSession,
    role: str | None,
    is_active: bool | None,
    page: int,
    page_size: int,
) -> PaginatedAdminUsersResponse:
    """
    List all users with optional filters.

    Filters:
    - role: "patient" | "doctor" | "admin"
    - is_active: True | False

    Returns paginated list ordered by created_at DESC (newest first).
    """
    query = select(User)

    if role is not None:
        try:
            role_enum = UserRole(role)
        except ValueError:
            raise BadRequestException(
                message=f"Invalid role filter '{role}'. Must be: patient, doctor, admin"
            )
        query = query.where(User.role == role_enum)

    if is_active is not None:
        query = query.where(User.is_active == is_active)

    # Total count (without pagination)
    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    # Paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        query.order_by(User.created_at.desc()).offset(offset).limit(page_size)
    )
    users = list(result.scalars().all())

    items = [
        AdminUserItem(
            id=u.id,
            email=u.email,
            full_name=u.full_name,
            role=u.role.value,
            is_active=u.is_active,
            is_verified=u.is_verified,
            created_at=u.created_at,
        )
        for u in users
    ]

    return PaginatedAdminUsersResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


async def get_user(db: AsyncSession, user_id: str) -> AdminUserDetail:
    """
    Fetch a single user with their role-specific profile.

    Raises 404 if user not found.
    """
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .options(
            selectinload(User.patient_profile),
            selectinload(User.doctor_profile),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise UserNotFoundException(message=f"No user found with id: {user_id}")

    patient_profile_out = None
    if user.patient_profile:
        pp = user.patient_profile
        patient_profile_out = AdminPatientProfileOut(
            patient_code=pp.patient_code,
            date_of_birth=pp.date_of_birth.isoformat() if pp.date_of_birth else None,
            gender=pp.gender,
            phone=pp.phone,
        )

    doctor_profile_out = None
    if user.doctor_profile:
        dp = user.doctor_profile
        doctor_profile_out = AdminDoctorProfileOut(
            specialization=dp.specialization,
            license_number=dp.license_number,
            clinic_name=dp.clinic_name,
        )

    return AdminUserDetail(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        is_active=user.is_active,
        is_verified=user.is_verified,
        created_at=user.created_at,
        patient_profile=patient_profile_out,
        doctor_profile=doctor_profile_out,
    )


async def update_user(
    db: AsyncSession,
    user_id: str,
    is_active: bool | None,
    is_verified: bool | None,
    role: str | None,
) -> AdminUserDetail:
    """
    Update account state fields for a user.

    Any combination of is_active, is_verified, and role may be changed.
    Raises 404 if user not found.
    Raises 400 if role value is invalid.
    """
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .options(
            selectinload(User.patient_profile),
            selectinload(User.doctor_profile),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise UserNotFoundException(message=f"No user found with id: {user_id}")

    if is_active is not None:
        action = "activated" if is_active else "suspended"
        user.is_active = is_active
        logger.info(f"admin_user_{action}", user_id=user_id)

    if is_verified is not None:
        user.is_verified = is_verified
        logger.info("admin_user_verification_updated", user_id=user_id, is_verified=is_verified)

    if role is not None:
        try:
            role_enum = UserRole(role)
        except ValueError:
            raise BadRequestException(
                message=f"Invalid role '{role}'. Must be: patient, doctor, admin"
            )
        logger.warning(
            "admin_user_role_changed",
            user_id=user_id,
            old_role=user.role.value,
            new_role=role,
        )
        user.role = role_enum

    # Re-fetch with profile to build response (session still has changes)
    return await get_user(db, user_id)


# ================================================================== #
# Case Management
# ================================================================== #

async def list_cases(
    db: AsyncSession,
    ai_status: str | None,
    clinical_status: str | None,
    page: int,
    page_size: int,
) -> PaginatedAdminCasesResponse:
    """
    List all cases across all patients with optional filters.

    Loads patient + doctor names via eager loading.
    Returns paginated list ordered by created_at DESC.
    """
    query = select(Case).options(
        selectinload(Case.patient),
        selectinload(Case.doctor),
    )

    if ai_status is not None:
        try:
            ai_enum = AiStatus(ai_status)
        except ValueError:
            raise BadRequestException(
                message=f"Invalid ai_status filter '{ai_status}'. "
                        "Must be: pending, processing, completed, failed"
            )
        query = query.where(Case.ai_status == ai_enum)

    if clinical_status is not None:
        from src.models.case import ClinicalStatus
        try:
            clinical_enum = ClinicalStatus(clinical_status)
        except ValueError:
            raise BadRequestException(
                message=f"Invalid clinical_status filter '{clinical_status}'. "
                        "Must be: active, follow_up_available, monitoring, resolved"
            )
        query = query.where(Case.clinical_status == clinical_enum)

    # Count
    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    # Paginated
    offset = (page - 1) * page_size
    result = await db.execute(
        query.order_by(Case.created_at.desc()).offset(offset).limit(page_size)
    )
    cases = list(result.scalars().all())

    items = [
        AdminCaseItem(
            id=c.id,
            patient_id=c.patient_id,
            patient_name=c.patient.full_name,
            doctor_id=c.doctor_id,
            doctor_name=c.doctor.full_name if c.doctor else None,
            ai_status=c.ai_status.value,
            clinical_status=c.clinical_status.value,
            consultation_type=c.consultation_type.value,
            created_at=c.created_at,
        )
        for c in cases
    ]

    return PaginatedAdminCasesResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


# ================================================================== #
# AI Settings
# ================================================================== #

async def get_ai_settings() -> AiSettingsResponse:
    """
    Return the current effective AI model configuration.

    Shows both the live values (Redis override or .env default)
    and what the .env defaults are — so the admin can see at a glance
    whether any runtime overrides are active.
    """
    from src.ai import model_registry
    from src.config import settings

    live = await model_registry.async_get_all()

    return AiSettingsResponse(
        gemini_model=live["gemini_model"],
        openai_model=live["openai_model"],
        deepseek_model=live["deepseek_model"],
        default_provider=live["default_provider"],
        env_defaults={
            "gemini_model":     settings.GEMINI_MODEL,
            "openai_model":     settings.OPENAI_MODEL,
            "deepseek_model":   settings.DEEPSEEK_MODEL,
            "default_provider": settings.DEFAULT_LLM_PROVIDER,
        },
    )


async def update_ai_settings(request: UpdateAiSettingsRequest) -> AiSettingsResponse:
    """
    Apply runtime AI model overrides. Each field is optional.

    - Sending a string value sets a Redis override → takes effect immediately
      for ALL processes (API server + Celery workers).
    - Sending null resets that setting to the .env default (deletes Redis key).

    Returns the new effective configuration after applying changes.
    """
    from src.ai import model_registry

    mapping = {
        "gemini_model":    request.gemini_model,
        "openai_model":    request.openai_model,
        "deepseek_model":  request.deepseek_model,
        "default_provider": request.default_provider,
    }

    for key, value in mapping.items():
        if value is not None:
            await model_registry.async_set_ai_setting(key, value)
            logger.info("admin_ai_setting_updated", key=key, value=value)
        # None means "not sent" — we don't reset unless explicitly set to null.
        # (Pydantic treats missing fields and null differently via model_fields_set)

    # Reset fields explicitly set to null by the caller
    for key in request.model_fields_set:
        if getattr(request, key) is None:
            await model_registry.async_reset_ai_setting(key)
            logger.info("admin_ai_setting_reset", key=key)

    return await get_ai_settings()


# ================================================================== #
# Platform Statistics
# ================================================================== #

async def get_stats(db: AsyncSession) -> AdminStatsResponse:
    """
    Compute platform-wide aggregated statistics.

    All counts are calculated in a single DB round-trip where possible.
    """
    # User counts
    total_users = (await db.execute(select(func.count(User.id)))).scalar_one()
    total_patients = (await db.execute(
        select(func.count(User.id)).where(User.role == UserRole.PATIENT)
    )).scalar_one()
    total_doctors = (await db.execute(
        select(func.count(User.id)).where(User.role == UserRole.DOCTOR)
    )).scalar_one()
    total_admins = (await db.execute(
        select(func.count(User.id)).where(User.role == UserRole.ADMIN)
    )).scalar_one()

    # Case counts
    total_cases = (await db.execute(select(func.count(Case.id)))).scalar_one()
    cases_ai_pending = (await db.execute(
        select(func.count(Case.id)).where(Case.ai_status == AiStatus.PENDING)
    )).scalar_one()
    cases_ai_processing = (await db.execute(
        select(func.count(Case.id)).where(Case.ai_status == AiStatus.PROCESSING)
    )).scalar_one()
    cases_ai_completed = (await db.execute(
        select(func.count(Case.id)).where(Case.ai_status == AiStatus.COMPLETED)
    )).scalar_one()
    cases_ai_failed = (await db.execute(
        select(func.count(Case.id)).where(Case.ai_status == AiStatus.FAILED)
    )).scalar_one()

    # Report count
    total_reports = (await db.execute(select(func.count(CaseReport.id)))).scalar_one()

    return AdminStatsResponse(
        total_users=total_users,
        total_patients=total_patients,
        total_doctors=total_doctors,
        total_admins=total_admins,
        total_cases=total_cases,
        cases_ai_pending=cases_ai_pending,
        cases_ai_processing=cases_ai_processing,
        cases_ai_completed=cases_ai_completed,
        cases_ai_failed=cases_ai_failed,
        total_reports=total_reports,
    )
