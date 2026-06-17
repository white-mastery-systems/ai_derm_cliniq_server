"""
users/service.py — User Profile Business Logic
================================================

Three operations:
1. get_profile   — load User + role-specific profile (no lazy load)
2. update_profile — apply PATCH fields to User + PatientProfile/DoctorProfile
3. soft_delete   — set is_active=False (not a hard DB delete)

ASYNC RELATIONSHIP LOADING
---------------------------
SQLAlchemy async does not support lazy loading. If we access
`user.patient_profile` without explicitly loading it, we get MissingGreenlet.

Solution: use `selectinload()` in the SELECT query so SQLAlchemy fetches
the relationship in a second SQL IN-query within the same async context.
This is the standard async-safe pattern.

PATCH SEMANTICS
---------------
We only apply fields that were explicitly sent in the request.
Pydantic's model_dump(exclude_none=True) gives us only the non-None fields.
We apply shared fields (full_name, phone) to User.
Role-specific fields go to the appropriate profile row.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.exceptions import FileTooLargeException, InvalidFileTypeException, UserNotFoundException
from src.logger import get_logger
from src.models.doctor_profile import DoctorProfile
from src.models.patient_profile import PatientProfile
from src.models.user import User, UserRole
from src.storage import gcs
from src.users.schemas import AvatarUploadResponse, DeviceTokenResponse, PatientByCodeResponse, ProfileUpdateRequest, UserProfileResponse

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

async def _load_user_with_profile(db: AsyncSession, user_id: str) -> User:
    """
    Load a User and eagerly fetch the appropriate profile relationship.

    selectinload issues one extra SELECT … WHERE user_id IN (…) query.
    It is async-safe and avoids MissingGreenlet from lazy loading.
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
        raise UserNotFoundException()
    return user


async def _resolve_avatar_url(path_or_url: str | None) -> str | None:
    """
    Return a fresh 60-minute signed URL if the stored value is a GCS path.
    Legacy rows that already contain a full https:// URL are returned as-is
    until the URL naturally expires (max 7 days from when it was stored).
    Returns None if GCS is not configured or the signed URL call fails.
    """
    if path_or_url and path_or_url.startswith("avatars/"):
        try:
            return await asyncio.to_thread(gcs.get_signed_url, path_or_url, 60)
        except Exception:
            return None
    return path_or_url


def _build_profile_response(user: User) -> UserProfileResponse:
    """
    Construct UserProfileResponse without accessing any lazy-loaded attributes.
    All relationships must already be loaded by _load_user_with_profile.
    """
    return UserProfileResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        is_active=user.is_active,
        is_verified=user.is_verified,
        patient_profile=user.patient_profile if user.role == UserRole.PATIENT else None,
        doctor_profile=user.doctor_profile if user.role == UserRole.DOCTOR else None,
    )


# ------------------------------------------------------------------ #
# get_profile
# ------------------------------------------------------------------ #

async def get_profile(db: AsyncSession, user_id: str) -> UserProfileResponse:
    """
    Return the full profile for the given user_id.

    Used by: GET /api/v1/users/me
    """
    user = await _load_user_with_profile(db, user_id)
    response = _build_profile_response(user)
    if response.patient_profile:
        response.patient_profile.avatar_url = await _resolve_avatar_url(response.patient_profile.avatar_url)
    if response.doctor_profile:
        response.doctor_profile.avatar_url = await _resolve_avatar_url(response.doctor_profile.avatar_url)
    return response


# ------------------------------------------------------------------ #
# update_profile
# ------------------------------------------------------------------ #

async def update_profile(
    db: AsyncSession,
    user_id: str,
    request: ProfileUpdateRequest,
) -> UserProfileResponse:
    """
    Apply PATCH updates to User + profile row.

    PATCH semantics: only explicitly provided fields are written.
    Role-specific fields silently ignored for the wrong role.

    Used by: PATCH /api/v1/users/me
    """
    user = await _load_user_with_profile(db, user_id)

    # --- Shared fields (apply to User row) ---
    if request.full_name is not None:
        user.full_name = request.full_name
    if request.phone is not None:
        _apply_phone(user, request.phone)

    # --- Role-specific fields ---
    if user.role == UserRole.PATIENT:
        _apply_patient_fields(user.patient_profile, request)
    elif user.role == UserRole.DOCTOR:
        _apply_doctor_fields(user.doctor_profile, request)

    logger.info("profile_updated", user_id=user_id, role=user.role.value)
    response = _build_profile_response(user)
    if response.patient_profile:
        response.patient_profile.avatar_url = await _resolve_avatar_url(response.patient_profile.avatar_url)
    if response.doctor_profile:
        response.doctor_profile.avatar_url = await _resolve_avatar_url(response.doctor_profile.avatar_url)
    return response


def _apply_phone(user: User, phone: str) -> None:
    """Phone lives on the profile row, not on User. Route to the right model."""
    if user.patient_profile:
        user.patient_profile.phone = phone
    elif user.doctor_profile:
        user.doctor_profile.phone = phone if hasattr(user.doctor_profile, "phone") else None


def _apply_patient_fields(profile: PatientProfile | None, req: ProfileUpdateRequest) -> None:
    if profile is None:
        return
    if req.date_of_birth is not None:
        profile.date_of_birth = req.date_of_birth
    if req.gender is not None:
        profile.gender = req.gender


def _apply_doctor_fields(profile: DoctorProfile | None, req: ProfileUpdateRequest) -> None:
    if profile is None:
        return
    if req.specialization is not None:
        profile.specialization = req.specialization
    if req.license_number is not None:
        profile.license_number = req.license_number
    if req.clinic_name is not None:
        profile.clinic_name = req.clinic_name
    if req.notifications_enabled is not None:
        profile.notifications_enabled = req.notifications_enabled


# ------------------------------------------------------------------ #
# get_patient_by_code
# ------------------------------------------------------------------ #

async def get_patient_by_code(
    db: AsyncSession,
    patient_code: str,
) -> PatientByCodeResponse:
    """
    Look up a patient by their patient code (e.g. PAT-ABCD1234).
    Used by the doctor's "Enter Patient Code" screen.

    Returns basic patient info safe for a doctor to view before
    being assigned to a case.

    Raises UserNotFoundException if code not found.
    """
    result = await db.execute(
        select(PatientProfile)
        .where(PatientProfile.patient_code == patient_code.upper())
        .options(selectinload(PatientProfile.user))
    )
    profile = result.scalar_one_or_none()

    if profile is None or not profile.user.is_active:
        raise UserNotFoundException(message=f"No patient found with code: {patient_code}")

    return PatientByCodeResponse(
        user_id=profile.user_id,
        full_name=profile.user.full_name,
        patient_code=profile.patient_code,
        date_of_birth=profile.date_of_birth,
        gender=profile.gender,
        avatar_url=await _resolve_avatar_url(profile.avatar_url),
    )


# ------------------------------------------------------------------ #
# update_device_token
# ------------------------------------------------------------------ #

async def update_device_token(db: AsyncSession, user_id: str, fcm_token: str) -> DeviceTokenResponse:
    """
    Store or replace the FCM device token for push notifications.

    Called when the Flutter app detects its FCM token has changed (e.g.
    after reinstall) without the user needing to log out and back in.

    Used by: POST /api/v1/users/me/device-token
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotFoundException()

    user.fcm_token = fcm_token
    logger.info("device_token_updated", user_id=user_id)
    return DeviceTokenResponse()


async def clear_device_token(db: AsyncSession, user_id: str) -> None:
    """Clear FCM token on logout so the device stops receiving push notifications."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.fcm_token = None
        logger.info("device_token_cleared", user_id=user_id)


# ------------------------------------------------------------------ #
# soft_delete
# ------------------------------------------------------------------ #

async def soft_delete(db: AsyncSession, user_id: str) -> None:
    """
    Soft-delete an account by setting is_active=False.

    WHY SOFT DELETE?
    -----------------
    Hard-deleting a user cascades to cases, images, messages, reports —
    destroying permanent medical records. Soft-delete preserves the data
    while preventing the user from logging in.

    An admin can reactivate the account if needed.

    Used by: DELETE /api/v1/users/me
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotFoundException()

    user.is_active = False
    logger.info("user_soft_deleted", user_id=user_id)


# ------------------------------------------------------------------ #
# upload_avatar
# ------------------------------------------------------------------ #

_ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/jpg", "image/png"}
_MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB


async def upload_avatar(
    db: AsyncSession,
    user: User,
    file_bytes: bytes,
    _filename: str,
    content_type: str,
) -> AvatarUploadResponse:
    """
    Upload a profile avatar to GCS and save the signed URL to the user's profile.

    Validates:
    - Content type must be jpg/jpeg/png
    - File size must not exceed 5 MB

    GCS path: avatars/{user_id}.{ext}
    Existing avatar is silently overwritten (same path).

    Used by: POST /api/v1/users/me/avatar
    """
    if content_type not in _ALLOWED_AVATAR_TYPES:
        raise InvalidFileTypeException(
            message=f"Invalid file type '{content_type}'. Allowed: jpg, jpeg, png"
        )

    if len(file_bytes) > _MAX_AVATAR_BYTES:
        raise FileTooLargeException(
            message=f"File too large ({len(file_bytes) // 1024} KB). Max allowed: 5 MB"
        )

    # Derive extension from content_type (always reliable vs filename)
    ext = "jpg" if content_type in {"image/jpeg", "image/jpg"} else "png"
    gcs_path = f"avatars/{user.id}.{ext}"

    # GCS calls are synchronous — run in thread pool to avoid blocking the event loop
    await asyncio.to_thread(gcs.upload_file, gcs_path, file_bytes, content_type)

    # Store the GCS path (not a signed URL) so the URL never expires in the DB.
    # A fresh signed URL is generated on every profile read by _resolve_avatar_url.
    user_with_profile = await _load_user_with_profile(db, user.id)

    if user_with_profile.role == UserRole.PATIENT and user_with_profile.patient_profile:
        user_with_profile.patient_profile.avatar_url = gcs_path
    elif user_with_profile.role == UserRole.DOCTOR and user_with_profile.doctor_profile:
        user_with_profile.doctor_profile.avatar_url = gcs_path

    # Return a fresh 60-minute signed URL so Flutter can display the image immediately
    signed_url = await asyncio.to_thread(gcs.get_signed_url, gcs_path, 60)
    logger.info("avatar_uploaded", user_id=user.id, gcs_path=gcs_path)
    return AvatarUploadResponse(avatar_url=signed_url)
