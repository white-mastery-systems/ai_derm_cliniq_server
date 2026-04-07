"""
admin/controller.py — Admin Dashboard HTTP Endpoints
=====================================================

All routes prefixed /api/v1/admin (set in src/api.py).

ROUTES
------
GET   /users              → Paginated user list (filter by role/is_active)
GET   /users/{user_id}    → Full user detail with profile
PATCH /users/{user_id}    → Update account state (activate/suspend/verify/role)
GET   /cases              → Paginated case list (filter by ai_status/clinical_status)
GET   /stats              → Platform-wide usage statistics

ALL ROUTES: ADMIN ONLY
-----------------------
Every endpoint in this controller requires the `require_admin` dependency.
Non-admins receive 403 before any business logic runs.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin import service
from src.admin.schemas import (
    AdminStatsResponse,
    AdminUpdateUserRequest,
    AdminUserDetail,
    AiSettingsResponse,
    PaginatedAdminCasesResponse,
    PaginatedAdminUsersResponse,
    UpdateAiSettingsRequest,
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
