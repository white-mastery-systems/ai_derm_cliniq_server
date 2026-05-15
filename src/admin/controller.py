"""
admin/controller.py — Admin Dashboard HTTP Endpoints
=====================================================

All routes prefixed /api/v1/admin (set in src/api.py).

ROUTES
------
GET    /users                             → Paginated user list (filter by role/is_active)
GET    /users/{user_id}                   → Full user detail with profile
PATCH  /users/{user_id}                   → Update account state (activate/suspend/verify/role)
DELETE /users/{user_id}                   → Permanently delete a user
POST   /users/{user_id}/resend-verification → Resend email verification OTP
GET    /cases                             → Paginated case list (filter by ai_status/clinical_status)
GET    /cases/{case_id}                   → Full case detail
GET    /stats                             → Platform-wide usage statistics
GET    /doctors                           → List all doctors (paginated)
GET    /doctors/pending                   → List pending approval doctors
POST   /doctors/{user_id}/approve         → Approve doctor (activate account + email)
POST   /doctors/{user_id}/reject          → Reject doctor (delete account + email)
GET    /prompts                           → List all AI prompt overrides
PATCH  /prompts/{key}                     → Set a prompt override in Redis
DELETE /prompts/{key}                     → Reset a prompt to its hardcoded default
GET    /ai-settings                       → Get current AI model configuration
PATCH  /ai-settings                       → Update AI model configuration at runtime

ALL ROUTES: ADMIN ONLY
-----------------------
Every endpoint in this controller requires the `require_admin` dependency.
Non-admins receive 403 before any business logic runs.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin import service
from src.admin.schemas import (
    AdminCaseDetail,
    AdminStatsResponse,
    AdminUpdateUserRequest,
    AdminUserDetail,
    AiSettingsResponse,
    CaseAuditLogResponse,
    PaginatedAdminCasesResponse,
    PaginatedAdminDoctorsResponse,
    PaginatedAdminUsersResponse,
    PromptsListResponse,
    PromptItem,
    RejectDoctorRequest,
    UpdateAiSettingsRequest,
    UpdatePromptRequest,
)
from src.auth.dependencies import require_admin
from src.database.core import get_async_session
from src.models.user import User

router = APIRouter()


@router.get(
    "/users",
    response_model=PaginatedAdminUsersResponse,
    status_code=status.HTTP_200_OK,
    summary="List all users (admin only)",
)
async def list_users(
    role: str | None = Query(default=None, description="Filter by role: patient, doctor, admin"),
    is_active: bool | None = Query(default=None, description="Filter by account state"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> PaginatedAdminUsersResponse:
    """
    List all users with optional filters.

    Results are ordered newest first.
    Returns 400 if an invalid role filter value is provided.
    """
    return await service.list_users(db, role, is_active, page, page_size)


@router.get(
    "/users/{user_id}",
    response_model=AdminUserDetail,
    status_code=status.HTTP_200_OK,
    summary="Get user detail (admin only)",
)
async def get_user(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> AdminUserDetail:
    """
    Fetch a single user's full profile including role-specific data.

    Returns 404 if the user does not exist.
    """
    return await service.get_user(db, user_id)


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserDetail,
    status_code=status.HTTP_200_OK,
    summary="Update user account state (admin only)",
)
async def update_user(
    user_id: str,
    request: AdminUpdateUserRequest,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> AdminUserDetail:
    """
    Update a user's account state.

    All fields are optional — send only what needs changing:
    - `is_active: false` → suspend account (user cannot log in)
    - `is_active: true`  → reactivate account
    - `is_verified: true` → mark email as verified
    - `role`             → change role (use with caution on active cases)

    Returns 404 if the user does not exist.
    Returns 400 if the role value is invalid.
    """
    return await service.update_user(
        db,
        user_id,
        is_active=request.is_active,
        is_verified=request.is_verified,
        role=request.role,
    )


@router.get(
    "/cases",
    response_model=PaginatedAdminCasesResponse,
    status_code=status.HTTP_200_OK,
    summary="List all cases (admin only)",
)
async def list_cases(
    ai_status: str | None = Query(
        default=None,
        description="Filter: pending, processing, completed, failed",
    ),
    clinical_status: str | None = Query(
        default=None,
        description="Filter: active, follow_up_available, monitoring, resolved",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> PaginatedAdminCasesResponse:
    """
    List all cases across all patients.

    Includes patient name and assigned doctor name in each item.
    Ordered newest first. Returns 400 for invalid filter values.
    """
    return await service.list_cases(db, ai_status, clinical_status, page, page_size)


@router.get(
    "/stats",
    response_model=AdminStatsResponse,
    status_code=status.HTTP_200_OK,
    summary="Platform statistics (admin only)",
)
async def get_stats(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> AdminStatsResponse:
    """
    Aggregated platform statistics for the admin dashboard:
    - User counts by role
    - Case counts by AI status
    - Total reports generated
    """
    return await service.get_stats(db)


# ------------------------------------------------------------------ #
# AI Model Settings
# ------------------------------------------------------------------ #

@router.get(
    "/ai-settings",
    response_model=AiSettingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current AI model configuration (admin only)",
    description=(
        "Returns the live effective model names for each AI provider. "
        "Each value shows the active override (set via PATCH) or the .env default. "
        "Also returns env_defaults so you can see what the .env baseline is."
    ),
)
async def get_ai_settings(
    _admin: User = Depends(require_admin),
) -> AiSettingsResponse:
    return await service.get_ai_settings()


@router.patch(
    "/ai-settings",
    response_model=AiSettingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Update AI model configuration at runtime (admin only)",
    description=(
        "Override the AI model names without restarting the server. "
        "Changes are stored in Redis and take effect immediately for all processes "
        "(API server + Celery workers). "
        "Send only the fields you want to change. "
        "Set a field to null to reset it to the .env default."
    ),
)
async def update_ai_settings(
    request: UpdateAiSettingsRequest,
    _admin: User = Depends(require_admin),
) -> AiSettingsResponse:
    return await service.update_ai_settings(request)


# ------------------------------------------------------------------ #
# Doctor Approval Workflow
# ------------------------------------------------------------------ #

@router.get(
    "/doctors",
    response_model=PaginatedAdminDoctorsResponse,
    status_code=status.HTTP_200_OK,
    summary="List all doctors (admin only)",
)
async def list_doctors(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> PaginatedAdminDoctorsResponse:
    """
    List all registered doctors with their profile details (specialization,
    license number, clinic name). Ordered newest first.
    """
    return await service.list_doctors(db, page, page_size, pending_only=False)


@router.get(
    "/doctors/pending",
    response_model=PaginatedAdminDoctorsResponse,
    status_code=status.HTTP_200_OK,
    summary="List doctors pending approval (admin only)",
)
async def list_pending_doctors(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> PaginatedAdminDoctorsResponse:
    """
    List doctors who have registered but not yet been approved
    (is_verified=False). These accounts cannot log in until approved.
    """
    return await service.list_doctors(db, page, page_size, pending_only=True)


@router.post(
    "/doctors/{user_id}/approve",
    response_model=AdminUserDetail,
    status_code=status.HTTP_200_OK,
    summary="Approve a doctor account (admin only)",
)
async def approve_doctor(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> AdminUserDetail:
    """
    Approve a pending doctor registration.

    - Sets `is_active=True` and `is_verified=True` so the doctor can log in.
    - Sends an approval confirmation email to the doctor.

    Returns 404 if user not found. Returns 400 if the user is not a doctor.
    """
    return await service.approve_doctor(db, user_id)


@router.post(
    "/doctors/{user_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reject and delete a doctor account (admin only)",
)
async def reject_doctor(
    user_id: str,
    request: RejectDoctorRequest,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """
    Reject a pending doctor registration and permanently delete their account.

    - Sends a rejection email (with optional reason) before deletion.
    - The account is permanently removed — no soft delete.

    Returns 404 if user not found. Returns 400 if the user is not a doctor.
    """
    await service.reject_doctor(db, user_id, reason=request.reason)


# ------------------------------------------------------------------ #
# Case Detail
# ------------------------------------------------------------------ #

@router.get(
    "/cases/{case_id}",
    response_model=AdminCaseDetail,
    status_code=status.HTTP_200_OK,
    summary="Get full case detail (admin only)",
)
async def get_case_detail(
    case_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> AdminCaseDetail:
    """
    Fetch full detail for a single case including patient/doctor info,
    AI status, clinical status, red flag results, and Q&A progress.

    Returns 404 if the case does not exist.
    """
    return await service.get_case_detail(db, case_id)


# ------------------------------------------------------------------ #
# Case Audit Log
# ------------------------------------------------------------------ #

@router.get(
    "/cases/{case_id}/audit-log",
    response_model=CaseAuditLogResponse,
    status_code=status.HTTP_200_OK,
    summary="Get audit log for a case (admin only)",
)
async def get_case_audit_log(
    case_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> CaseAuditLogResponse:
    """
    Return the full, ordered audit trail for a case.

    Each entry records what happened, when, and who (or what system) triggered it.
    actor_role = "system" means the event was triggered by the AI / Celery worker.

    Returns 404 if the case does not exist.
    """
    return await service.get_case_audit_log(db, case_id)


# ------------------------------------------------------------------ #
# User Deletion & Verification Resend
# ------------------------------------------------------------------ #

@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Permanently delete a user (admin only)",
)
async def delete_user(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """
    Permanently delete a user account and all cascaded data (cases, images,
    reports, reviews). This action is irreversible.

    Returns 404 if the user does not exist.
    """
    await service.delete_user(db, user_id)


@router.post(
    "/users/{user_id}/resend-verification",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Resend email verification OTP (admin only)",
)
async def resend_verification(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """
    Resend the 6-digit email verification OTP to an unverified user.

    Returns 404 if the user does not exist.
    Returns 400 if the user is already verified.
    """
    await service.resend_verification(db, user_id)


# ------------------------------------------------------------------ #
# AI Prompt Management
# ------------------------------------------------------------------ #

@router.get(
    "/prompts",
    response_model=PromptsListResponse,
    status_code=status.HTTP_200_OK,
    summary="List all AI prompt overrides (admin only)",
)
async def get_prompts(
    _admin: User = Depends(require_admin),
) -> PromptsListResponse:
    """
    Return all AI prompt keys with their current values.

    Each entry shows:
    - `key`: the prompt identifier used in code
    - `label`: human-readable name for the admin panel
    - `value`: the current Redis override (null if using the hardcoded default)
    - `has_override`: true if a Redis override is active

    Prompts not overridden fall back to the hardcoded defaults in the codebase.
    """
    return service.get_all_prompts()


@router.patch(
    "/prompts/{key}",
    response_model=PromptItem,
    status_code=status.HTTP_200_OK,
    summary="Set an AI prompt override (admin only)",
)
async def update_prompt(
    key: str,
    request: UpdatePromptRequest,
    _admin: User = Depends(require_admin),
) -> PromptItem:
    """
    Set a Redis override for an AI prompt.

    The new prompt takes effect immediately for all running processes
    (API server + Celery workers) without a restart.

    Returns 400 if `key` is not a valid prompt key.
    """
    try:
        return await service.update_prompt(key, request.value)
    except ValueError as exc:
        from src.exceptions import BadRequestException
        raise BadRequestException(message=str(exc))


@router.delete(
    "/prompts/{key}",
    response_model=PromptItem,
    status_code=status.HTTP_200_OK,
    summary="Reset an AI prompt to its hardcoded default (admin only)",
)
async def reset_prompt(
    key: str,
    _admin: User = Depends(require_admin),
) -> PromptItem:
    """
    Delete the Redis override for a prompt key.

    After this call the prompt method will use its hardcoded default value.
    Returns the prompt entry with `has_override=false` and `value=null`.

    Returns 400 if `key` is not a valid prompt key.
    """
    try:
        return await service.reset_prompt(key)
    except ValueError as exc:
        from src.exceptions import BadRequestException
        raise BadRequestException(message=str(exc))
