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

from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.admin.schemas import (
    AdminCaseDetail,
    AdminCaseItem,
    AdminDoctorItem,
    AdminDoctorProfileOut,
    AdminPatientProfileOut,
    AdminStatsResponse,
    AdminUserDetail,
    AdminUserItem,
    AiSettingsResponse,
    AuditLogEntry,
    CaseAuditLogResponse,
    PaginatedAdminCasesResponse,
    PaginatedAdminDoctorsResponse,
    PaginatedAdminUsersResponse,
    PromptHistoryEntry,
    PromptHistoryResponse,
    PromptItem,
    PromptsListResponse,
    UpdateAiSettingsRequest,
)
from src.exceptions import BadRequestException, CaseNotFoundException, UserNotFoundException
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

    from src.config import settings
    if user.email == settings.ADMIN_EMAIL:
        raise BadRequestException(
            message="The bootstrap admin account cannot be modified"
        )

    if is_active is not None:
        action = "activated" if is_active else "suspended"
        user.is_active = is_active
        now = datetime.now(tz=timezone.utc)
        if is_active and user.role == UserRole.DOCTOR:
            user.approved_at = now
        elif not is_active and user.role == UserRole.DOCTOR:
            user.rejected_at = now
        logger.info(f"admin_user_{action}", user_id=user_id)

    if is_verified is not None:
        user.is_verified = is_verified
        if is_verified:
            user.verified_at = datetime.now(tz=timezone.utc)
        logger.info("admin_user_verification_updated", user_id=user_id, is_verified=is_verified)

    if role is not None:
        try:
            role_enum = UserRole(role)
        except ValueError:
            raise BadRequestException(
                message=f"Invalid role '{role}'. Must be: patient, doctor, admin"
            )
        if role_enum == UserRole.ADMIN and user.role != UserRole.DOCTOR:
            raise BadRequestException(
                message="Only doctors can be promoted to admin"
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


# ================================================================== #
# Doctor Approval Workflow
# ================================================================== #

async def list_doctors(
    db: AsyncSession,
    page: int,
    page_size: int,
    pending_only: bool = False,
) -> PaginatedAdminDoctorsResponse:
    """
    List all doctors with their profile data.

    If pending_only=True, returns only doctors with is_active=False
    (awaiting admin approval). Uses is_active rather than is_verified so that
    Google OAuth doctors (is_verified=True, is_active=False) are included.
    """
    query = (
        select(User)
        .where(User.role == UserRole.DOCTOR)
        .options(selectinload(User.doctor_profile))
    )
    if pending_only:
        query = query.where(User.is_active == False)  # noqa: E712

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    result = await db.execute(
        query.order_by(User.created_at.desc()).offset(offset).limit(page_size)
    )
    doctors = list(result.scalars().all())

    items = []
    for u in doctors:
        dp = u.doctor_profile
        items.append(AdminDoctorItem(
            id=u.id,
            email=u.email,
            full_name=u.full_name,
            is_active=u.is_active,
            is_verified=u.is_verified,
            created_at=u.created_at,
            specialization=dp.specialization if dp else None,
            license_number=dp.license_number if dp else None,
            clinic_name=dp.clinic_name if dp else None,
        ))

    return PaginatedAdminDoctorsResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


async def approve_doctor(db: AsyncSession, user_id: str) -> AdminUserDetail:
    """
    Approve a pending doctor.

    Sets is_active=True and is_verified=True so the doctor can log in.
    Sends an approval confirmation email.
    Raises 404 if user not found, 400 if user is not a doctor.
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
    if user.role != UserRole.DOCTOR:
        raise BadRequestException(message="User is not a doctor")

    user.is_active = True
    user.is_verified = True
    await db.flush()

    from src.core.email import render_doctor_approved_email, send_email_async
    html = render_doctor_approved_email(name=user.full_name)
    await send_email_async(
        to_email=user.email,
        subject="AiDerm Cliniq — Your Account Has Been Approved",
        html_body=html,
    )

    try:
        from src.workers.tasks.notifications import notify_doctor_approved
        notify_doctor_approved.delay(doctor_id=user_id)
    except Exception as exc:
        logger.warning("notify_doctor_approved_enqueue_failed", error=str(exc))

    logger.info("admin_doctor_approved", user_id=user_id)

    return await get_user(db, user_id)


async def reject_doctor(db: AsyncSession, user_id: str, reason: str | None) -> None:
    """
    Reject a pending doctor and permanently delete their account.

    Sends a rejection email before deletion so the doctor knows their
    application was unsuccessful. Raises 404 if user not found,
    400 if user is not a doctor.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotFoundException(message=f"No user found with id: {user_id}")
    if user.role != UserRole.DOCTOR:
        raise BadRequestException(message="User is not a doctor")

    from src.core.email import render_doctor_rejected_email, send_email_async
    html = render_doctor_rejected_email(name=user.full_name, reason=reason)
    await send_email_async(
        to_email=user.email,
        subject="AiDerm Cliniq — Account Application Update",
        html_body=html,
    )

    try:
        from src.workers.tasks.notifications import notify_doctor_rejected
        notify_doctor_rejected.delay(doctor_id=user_id)
    except Exception as exc:
        logger.warning("notify_doctor_rejected_enqueue_failed", error=str(exc))

    await db.delete(user)
    logger.info("admin_doctor_rejected_and_deleted", user_id=user_id)


# ================================================================== #
# Case Detail
# ================================================================== #

async def get_case_detail(db: AsyncSession, case_id: str, admin_id: str | None = None) -> AdminCaseDetail:
    """
    Fetch full case detail for admin view.

    Writes a CASE_ACCESSED audit entry so every admin view of patient data
    is traceable. Raises 404 if case not found.
    """
    result = await db.execute(
        select(Case)
        .where(Case.id == case_id)
        .options(
            selectinload(Case.patient),
            selectinload(Case.doctor),
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    from src.models.audit_log import AuditEventType, CaseAuditLog
    db.add(CaseAuditLog(
        case_id=case_id,
        event_type=AuditEventType.CASE_ACCESSED,
        actor_id=admin_id,
        actor_role="admin",
    ))
    await db.commit()

    return AdminCaseDetail(
        id=case.id,
        case_number=case.case_number,
        patient_id=case.patient_id,
        patient_name=case.patient.full_name,
        doctor_id=case.doctor_id,
        doctor_name=case.doctor.full_name if case.doctor else None,
        consultation_type=case.consultation_type.value,
        has_visible_lesion=case.has_visible_lesion,
        is_for_self=case.is_for_self,
        dependent_name=case.dependent_name,
        dependent_relationship=case.dependent_relationship,
        body_location=case.body_location,
        presenting_complaint=case.presenting_complaint,
        case_summary=case.case_summary,
        case_title=case.case_title,
        symptom_tags=case.symptom_tags,
        ai_status=case.ai_status.value,
        clinical_status=case.clinical_status.value,
        clinical_status_changed_at=case.clinical_status_changed_at,
        red_flag_status=case.red_flag_status.value,
        red_flags=case.red_flags,
        red_flag_advice=case.red_flag_advice,
        question_round=case.question_round,
        consent_ai_analysis=case.consent_ai_analysis,
        consent_research=case.consent_research,
        created_at=case.created_at,
    )


# ================================================================== #
# User Deletion & Verification Resend
# ================================================================== #

async def delete_user(db: AsyncSession, user_id: str) -> None:
    """
    Permanently delete a user account and all cascaded data.

    Raises 404 if user not found.
    Raises 400 if attempting to delete the bootstrap admin.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotFoundException(message=f"No user found with id: {user_id}")

    from src.config import settings
    if user.email == settings.ADMIN_EMAIL:
        raise BadRequestException(
            message="The bootstrap admin account cannot be deleted"
        )

    await db.delete(user)
    logger.info("admin_user_deleted", user_id=user_id, role=user.role.value)


async def resend_verification(db: AsyncSession, user_id: str) -> None:
    """
    Resend the email verification OTP to an unverified user.

    Raises 404 if user not found. Raises 400 if already verified.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotFoundException(message=f"No user found with id: {user_id}")
    if user.is_verified:
        raise BadRequestException(message="User email is already verified")

    from src.auth.service import request_email_verification
    await request_email_verification(db, user)
    logger.info("admin_resend_verification", user_id=user_id)


# ================================================================== #
# AI Prompt Management
# ================================================================== #

def get_all_prompts() -> PromptsListResponse:
    """Return all prompt keys with their current Redis overrides and labels."""
    from src.ai.prompt_registry import get_all
    data = get_all()
    prompts = [
        PromptItem(
            key=v["key"],
            label=v["label"],
            value=v["value"],
            default_value=v["default_value"],
            has_override=v["has_override"],
        )
        for v in data.values()
    ]
    return PromptsListResponse(prompts=prompts)


async def update_prompt(key: str, value: str) -> PromptItem:
    """
    Set a Redis override for a prompt key.

    Raises ValueError (→ 400) if key is not a valid prompt key.
    """
    from src.ai import prompt_registry
    await prompt_registry.async_set_prompt(key, value)
    logger.info("admin_prompt_updated", key=key)
    entry = prompt_registry.get_all()[key]
    return PromptItem(
        key=entry["key"],
        label=entry["label"],
        value=entry["value"],
        default_value=entry["default_value"],
        has_override=entry["has_override"],
    )


async def reset_prompt(key: str) -> PromptItem:
    """
    Delete the Redis override for a prompt key, restoring the hardcoded default.

    Raises ValueError (→ 400) if key is not a valid prompt key.
    """
    from src.ai import prompt_registry
    await prompt_registry.async_reset_prompt(key)
    logger.info("admin_prompt_reset", key=key)
    entry = prompt_registry.get_all()[key]
    return PromptItem(
        key=entry["key"],
        label=entry["label"],
        value=entry["value"],
        default_value=entry["default_value"],
        has_override=entry["has_override"],
    )


async def get_prompt_history(key: str) -> PromptHistoryResponse:
    """
    Return the version history for a prompt key (newest first, up to 10 entries).

    Raises ValueError (→ 400) if key is not a valid prompt key.
    """
    from src.ai import prompt_registry
    history = await prompt_registry.async_get_history(key)
    return PromptHistoryResponse(
        key=key,
        history=[
            PromptHistoryEntry(value=e["value"], updated_at=e["updated_at"])
            for e in history
        ],
    )


async def rollback_prompt(key: str) -> PromptItem:
    """
    Restore the previous version of a prompt from history.

    Pops the most recent history entry and sets it as the current value.
    Raises ValueError (→ 400) if key is invalid or history is empty.
    """
    from src.ai import prompt_registry
    restored = await prompt_registry.async_rollback_prompt(key)
    if restored is None:
        raise ValueError(f"No history available for prompt key '{key}'")
    logger.info("admin_prompt_rolled_back", key=key)
    entry = prompt_registry.get_all()[key]
    return PromptItem(
        key=entry["key"],
        label=entry["label"],
        value=entry["value"],
        default_value=entry["default_value"],
        has_override=entry["has_override"],
    )


# ================================================================== #
# Case Audit Log
# ================================================================== #

async def get_case_audit_log(db: AsyncSession, case_id: str) -> CaseAuditLogResponse:
    """
    Return all audit log entries for a case, ordered oldest-first.

    Raises 404 if the case does not exist.
    The audit log is append-only — entries are never modified after insertion.
    """
    from src.models.audit_log import CaseAuditLog

    # Confirm the case exists
    case_exists = await db.execute(select(Case.id).where(Case.id == case_id))
    if case_exists.scalar_one_or_none() is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    result = await db.execute(
        select(CaseAuditLog)
        .where(CaseAuditLog.case_id == case_id)
        .order_by(CaseAuditLog.created_at.asc())
    )
    rows = list(result.scalars().all())

    items = [
        AuditLogEntry(
            id=row.id,
            case_id=row.case_id,
            event_type=row.event_type.value,
            actor_id=row.actor_id,
            actor_role=row.actor_role,
            event_data=row.event_data,
            created_at=row.created_at,
        )
        for row in rows
    ]

    return CaseAuditLogResponse(case_id=case_id, total=len(items), items=items)
