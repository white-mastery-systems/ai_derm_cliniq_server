"""
users/controller.py — User Profile HTTP Endpoints
===================================================

All routes prefixed /api/v1/users (set in src/api.py).

ROUTES
------
GET    /me   → return own profile (User + role-specific sub-profile)
PATCH  /me   → update own profile fields
DELETE /me   → soft-delete own account

AUTH
----
All routes require a valid Bearer JWT.
get_current_user dependency validates the token and returns the User ORM object.
The user is always operating on their OWN account — no user_id path param needed.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_doctor
from src.database.core import get_async_session
from src.models.user import User
from src.users import service
from src.users.schemas import PatientByCodeResponse, ProfileUpdateRequest, UserProfileResponse

router = APIRouter()


@router.get(
    "/me",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Get own profile",
    description=(
        "Returns the authenticated user's full profile including role-specific "
        "fields. Patients get patient_profile (patient_code, DOB, gender, phone). "
        "Doctors get doctor_profile (specialization, license_number, clinic_name)."
    ),
)
async def get_my_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> UserProfileResponse:
    return await service.get_profile(db, current_user.id)


@router.patch(
    "/me",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Update own profile",
    description=(
        "Update profile fields. Only send fields you want to change (PATCH semantics). "
        "Role-specific fields are ignored for the wrong role — no error. "
        "Avatar upload is handled separately via POST /users/me/avatar."
    ),
)
async def update_my_profile(
    request: ProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> UserProfileResponse:
    return await service.update_profile(db, current_user.id, request)


@router.get(
    "/by-code/{patient_code}",
    response_model=PatientByCodeResponse,
    status_code=status.HTTP_200_OK,
    summary="Look up a patient by their patient code (doctor only)",
    description=(
        "Used by the doctor's 'Enter Patient Code' screen. "
        "Returns basic patient info. "
        "Raises 404 if no active patient has that code."
    ),
)
async def get_patient_by_code(
    patient_code: str,
    _doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> PatientByCodeResponse:
    return await service.get_patient_by_code(db, patient_code)


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete own account",
    description=(
        "Sets is_active=False on the account. The user can no longer log in. "
        "All case data is preserved (medical records must not be hard-deleted). "
        "An admin can reactivate the account."
    ),
)
async def delete_my_account(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    await service.soft_delete(db, current_user.id)
