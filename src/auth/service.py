"""
auth/service.py — Auth Business Logic
======================================

This module contains all the "what happens during auth" logic.
Controllers (HTTP layer) call these functions; they never touch the DB directly.

SEPARATION OF CONCERNS
-----------------------
controller.py  →  HTTP concerns (parse request, return response, status codes)
service.py     →  Business logic (what does register/login/refresh actually DO?)
models/        →  DB schema (what is stored and how?)

WHY THIS MATTERS
-----------------
If we want to add email verification, OAuth with GitHub, or 2FA later,
we only change service.py — the controller stays the same.
The logic is also testable without HTTP overhead.

FUNCTIONS
---------
register_patient   → create User (PATIENT) + PatientProfile + RefreshToken row
register_doctor    → create User (DOCTOR) + DoctorProfile + RefreshToken row
login              → verify credentials, create RefreshToken row, return tokens
refresh_tokens     → validate + rotate refresh token pair
logout             → revoke a single RefreshToken row
google_auth        → verify Google ID token, upsert User + profile, return tokens
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.google import verify_google_access_token, verify_google_id_token
from src.auth.jwt import (
    create_access_token,
    get_refresh_token_expiry,
)
from src.auth.schemas import (
    DoctorRegisterRequest,
    LoginRequest,
    PatientRegisterRequest,
    TokenResponse,
)
from src.auth.security import (
    generate_patient_code,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from src.exceptions import (
    BadRequestException,
    DoctorPendingApprovalException,
    EmailAlreadyRegisteredException,
    InvalidCredentialsException,
    InvalidTokenException,
    UnauthorizedException,
)
from src.logger import get_logger
from src.models.doctor_profile import DoctorProfile
from src.models.patient_profile import PatientProfile
from src.models.refresh_token import RefreshToken
from src.models.user import User, UserRole
from src.models.verification_token import TokenPurpose, VerificationToken

if TYPE_CHECKING:
    pass

# Expiry constants
_PASSWORD_RESET_EXPIRY_MINUTES = 15
_EMAIL_VERIFY_EXPIRY_MINUTES = 30

# Roles permitted via OAuth sign-in — admin can never be created this way
_ALLOWED_OAUTH_ROLES: frozenset[str] = frozenset({UserRole.PATIENT.value, UserRole.DOCTOR.value})

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# Internal helpers
# ------------------------------------------------------------------ #

def _build_token_response(user: User, raw_refresh: str) -> TokenResponse:
    """Assemble the TokenResponse from a User and a raw refresh token string."""
    access = create_access_token(user_id=user.id, role=user.role.value)
    return TokenResponse(
        access_token=access,
        refresh_token=raw_refresh,
        token_type="bearer",
        role=user.role.value,
        user_id=user.id,
    )


async def _create_refresh_token_row(
    db: AsyncSession,
    user_id: str,
    raw_token: str,
) -> None:
    """
    Persist a hashed refresh token to the DB.

    Only the SHA-256 hash is stored — the raw token is never persisted.
    On logout or rotation, the row is revoked (not deleted) for audit trails.
    """
    token_row = RefreshToken(
        user_id=user_id,
        token_hash=hash_refresh_token(raw_token),
        expires_at=get_refresh_token_expiry(),
        revoked=False,
    )
    db.add(token_row)


async def _get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Return the User with this email, or None if not found."""
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def _generate_unique_patient_code(db: AsyncSession) -> str:
    """
    Generate a patient code that is not already in the DB.

    Collision probability: ~1 in 456_976_000 per attempt.
    Retrying up to 10 times makes a collision effectively impossible.
    """
    for _ in range(10):
        code = generate_patient_code()
        existing = await db.execute(
            select(PatientProfile).where(PatientProfile.patient_code == code)
        )
        if existing.scalar_one_or_none() is None:
            return code
    # This path is astronomically unlikely in practice
    raise RuntimeError("Failed to generate a unique patient code after 10 attempts")


# ------------------------------------------------------------------ #
# Registration
# ------------------------------------------------------------------ #

async def register_patient(
    db: AsyncSession,
    request: PatientRegisterRequest,
) -> tuple[User, TokenResponse]:
    """
    Create a new patient account.

    Steps:
    1. Check email uniqueness
    2. Create User row (PATIENT role)
    3. Generate unique patient_code
    4. Create PatientProfile row
    5. Issue refresh token
    6. Return (user, token_response)

    Raises:
        EmailAlreadyRegisteredException — if email is taken
    """
    # 1. Email uniqueness check
    if await _get_user_by_email(db, request.email):
        raise EmailAlreadyRegisteredException()

    # 2. Create User
    # hash_password is synchronous bcrypt (~400ms) — run in thread pool so the
    # event loop stays free to handle other requests while hashing completes.
    hashed_pw = await asyncio.to_thread(hash_password, request.password)
    user = User(
        email=request.email,
        full_name=request.full_name,
        role=UserRole.PATIENT,
        password_hash=hashed_pw,
        is_active=True,
        is_verified=False,
        fcm_token=request.fcm_token or None,
    )
    db.add(user)
    await db.flush()  # Assigns user.id without committing

    # 3 + 4. Generate patient_code and create PatientProfile
    patient_code = await _generate_unique_patient_code(db)
    profile = PatientProfile(
        user_id=user.id,
        patient_code=patient_code,
        date_of_birth=request.date_of_birth,
        gender=request.gender,
    )
    db.add(profile)

    # 5. Issue refresh token
    raw_refresh = generate_refresh_token()
    await _create_refresh_token_row(db, user.id, raw_refresh)

    # db.commit() is called by the session dependency after this returns
    logger.info("patient_registered", user_id=user.id, email=user.email)

    return user, _build_token_response(user, raw_refresh), patient_code


async def register_doctor(
    db: AsyncSession,
    request: DoctorRegisterRequest,
) -> User:
    """
    Create a new doctor account — email verification required, then admin approval.

    Steps:
    1. Check email uniqueness
    2. Create User row (DOCTOR role, is_active=False, is_verified=False)
    3. Create DoctorProfile row
    4. Generate OTP and email it for email verification
    5. Return User — NO tokens issued until email verified + admin approves

    Raises:
        EmailAlreadyRegisteredException — if email is taken
    """
    # 1. Email uniqueness check
    if await _get_user_by_email(db, request.email):
        raise EmailAlreadyRegisteredException()

    # 2. Create User (is_active=False until admin approves, is_verified=False until OTP confirmed)
    hashed_pw = await asyncio.to_thread(hash_password, request.password)
    user = User(
        email=request.email,
        full_name=request.full_name,
        role=UserRole.DOCTOR,
        password_hash=hashed_pw,
        is_active=False,
        is_verified=False,
        fcm_token=request.fcm_token or None,
    )
    db.add(user)
    await db.flush()

    # 3. Create DoctorProfile
    profile = DoctorProfile(
        user_id=user.id,
        specialization=request.specialization,
        license_number=request.license_number,
        clinic_name=request.clinic_name,
        notifications_enabled=True,
    )
    db.add(profile)

    # 4. Generate OTP and send verification email
    otp = await _create_verification_token(
        db, user.id, TokenPurpose.EMAIL_VERIFY,
        expiry_minutes=_EMAIL_VERIFY_EXPIRY_MINUTES,
    )
    try:
        from src.core.email import render_doctor_registration_otp_email, send_email_async
        html = render_doctor_registration_otp_email(name=request.full_name, otp=otp)
        await send_email_async(
            to_email=request.email,
            subject="AiDerm Cliniq — Verify Your Email",
            html_body=html,
        )
        logger.info("doctor_registration_otp_sent", user_id=user.id)
    except Exception as exc:
        logger.warning("doctor_registration_otp_email_failed", user_id=user.id, error=str(exc))

    logger.info("doctor_registered_pending_email_verification", user_id=user.id, email=user.email)

    return user


async def verify_doctor_email(db: AsyncSession, email: str, otp: str) -> None:
    """
    Confirm a doctor's email address using a 6-digit OTP sent at registration.

    Unlike the patient flow, this endpoint is unauthenticated — doctors have no
    access token yet because they cannot log in until admin approves them.

    On success, sets is_verified=True. The account remains is_active=False until
    an admin approves the doctor.

    Raises:
        InvalidTokenException — OTP not found, already used, or expired
    """
    user = await _get_user_by_email(db, email)
    if user is None or user.role != UserRole.DOCTOR:
        raise InvalidTokenException(message="OTP is invalid or has already been used")

    if user.is_verified:
        raise BadRequestException(message="Email address is already verified")

    await _redeem_otp(db, user.id, otp, TokenPurpose.EMAIL_VERIFY)
    user.is_verified = True
    logger.info("doctor_email_verified", user_id=user.id)

    # Notify admins now that the doctor has verified their email
    try:
        from src.workers.tasks.notifications import notify_admins_doctor_registered
        notify_admins_doctor_registered.delay(
            doctor_name=user.full_name,
            doctor_id=user.id,
        )
    except Exception as exc:
        logger.warning("notify_admins_doctor_registered_enqueue_failed", error=str(exc))


async def resend_doctor_verification(db: AsyncSession, email: str) -> None:
    """
    Resend the email verification OTP to a doctor who hasn't verified yet.

    Silent no-op if the email is not a registered doctor or is already verified —
    prevents user enumeration.
    """
    user = await _get_user_by_email(db, email)
    if user is None or user.role != UserRole.DOCTOR or user.is_verified:
        logger.info("resend_doctor_verification_noop", email=email)
        return

    otp = await _create_verification_token(
        db, user.id, TokenPurpose.EMAIL_VERIFY,
        expiry_minutes=_EMAIL_VERIFY_EXPIRY_MINUTES,
    )
    try:
        from src.core.email import render_doctor_registration_otp_email, send_email_async
        html = render_doctor_registration_otp_email(name=user.full_name, otp=otp)
        await send_email_async(
            to_email=email,
            subject="AiDerm Cliniq — Verify Your Email",
            html_body=html,
        )
        logger.info("doctor_verification_otp_resent", user_id=user.id)
    except Exception as exc:
        logger.warning("doctor_verification_otp_resend_failed", user_id=user.id, error=str(exc))


# ------------------------------------------------------------------ #
# Login
# ------------------------------------------------------------------ #

async def login(db: AsyncSession, request: LoginRequest) -> TokenResponse:
    """
    Authenticate with email + password.

    Steps:
    1. Look up user by email
    2. Verify password (bcrypt)
    3. Check account is active
    4. Create RefreshToken row
    5. Return TokenResponse

    Raises:
        InvalidCredentialsException — wrong email or password
        UnauthorizedException       — account is inactive

    SECURITY NOTE: We return the same error for wrong email AND wrong
    password. This prevents user enumeration attacks — an attacker
    cannot tell from the error whether the email exists.
    """
    user = await _get_user_by_email(db, request.email)

    # Wrong email OR no password (Google OAuth user trying to use password login)
    if not user or not user.password_hash:
        raise InvalidCredentialsException()

    is_valid = await asyncio.to_thread(verify_password, request.password, user.password_hash)
    if not is_valid:
        raise InvalidCredentialsException()

    if not user.is_active:
        if user.role == UserRole.DOCTOR:
            raise DoctorPendingApprovalException()
        raise UnauthorizedException(message="Account is suspended")

    # Always refresh fcm_token so the latest device is registered
    if request.fcm_token:
        user.fcm_token = request.fcm_token

    raw_refresh = generate_refresh_token()
    await _create_refresh_token_row(db, user.id, raw_refresh)

    logger.info("user_logged_in", user_id=user.id, role=user.role.value)

    return _build_token_response(user, raw_refresh)


# ------------------------------------------------------------------ #
# Token Refresh
# ------------------------------------------------------------------ #

async def refresh_tokens(db: AsyncSession, raw_refresh_token: str) -> TokenResponse:
    """
    Rotate a refresh token pair.

    TOKEN ROTATION PATTERN
    -----------------------
    Each refresh issues a NEW access + refresh token and revokes the old one.
    This limits the attack window: a stolen token is usable at most once
    before the legitimate user triggers a refresh and invalidates it.

    Steps:
    1. Decode JWT claims (validates signature + expiry)
    2. Look up the token hash in the DB (validates it hasn't been revoked)
    3. Revoke the old token row
    4. Issue a new pair
    5. Return TokenResponse

    Raises:
        InvalidTokenException — revoked or not found in DB
        UnauthorizedException — user account not found or suspended
    """
    # 1. Hash the raw token and look up in DB
    #    The token is an opaque random string — we never decode it as JWT.
    #    user_id comes from the DB row, not from a JWT claim.
    token_hash = hash_refresh_token(raw_refresh_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    token_row = result.scalar_one_or_none()

    if token_row is None or token_row.revoked:
        logger.warning("refresh_token_not_found_or_revoked")
        raise InvalidTokenException(message="Refresh token is invalid or has been revoked")

    # 2. Check expiry
    # SQLite stores timestamps without timezone; treat naive datetimes as UTC.
    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(tz=timezone.utc):
        raise InvalidTokenException(message="Refresh token has expired")

    # 3. Revoke old token
    user_id = token_row.user_id
    token_row.revoked = True
    token_row.revoked_at = datetime.now(tz=timezone.utc)

    # 4. Load user for role info
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user is None or not user.is_active:
        if user is not None and user.role == UserRole.DOCTOR:
            raise DoctorPendingApprovalException()
        raise UnauthorizedException(message="User account not found or suspended")

    # 5. Issue new pair
    new_raw_refresh = generate_refresh_token()
    await _create_refresh_token_row(db, user.id, new_raw_refresh)

    logger.info("tokens_rotated", user_id=user_id)

    return _build_token_response(user, new_raw_refresh)


# ------------------------------------------------------------------ #
# Logout
# ------------------------------------------------------------------ #

async def logout(db: AsyncSession, raw_refresh_token: str) -> None:
    """
    Revoke a refresh token so it can no longer be used.

    We don't invalidate the access token (stateless — it expires naturally).
    The client must discard the access token locally after logout.

    Silently succeeds if the token is already revoked or not found —
    this prevents information leakage about token state.

    Steps:
    1. Hash the token
    2. Find the DB row
    3. Mark as revoked
    """
    token_hash = hash_refresh_token(raw_refresh_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    token_row = result.scalar_one_or_none()

    if token_row and not token_row.revoked:
        token_row.revoked = True
        token_row.revoked_at = datetime.now(tz=timezone.utc)
        logger.info("user_logged_out", user_id=token_row.user_id)


# ------------------------------------------------------------------ #
# Google OAuth
# ------------------------------------------------------------------ #

async def google_auth(
    db: AsyncSession,
    role: str,
    id_token: str | None = None,
    access_token: str | None = None,
    fcm_token: str | None = None,
) -> TokenResponse:
    """
    Sign in or register via Google OAuth.

    Accepts either id_token (mobile) or access_token (Flutter web fallback).
    The GIS OAuth popup on Flutter web returns an access_token but not an id_token.

    `role` is only used when creating a NEW user — returning users keep
    their existing role regardless of what `role` is sent.
    """
    # 1. Verify token with Google — prefer id_token, fall back to access_token
    if id_token:
        google_info = await verify_google_id_token(id_token)
    else:
        google_info = await verify_google_access_token(access_token)  # type: ignore[arg-type]

    # 2. Look up by google_id
    result = await db.execute(
        select(User).where(User.google_id == google_info.google_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        # 3a. Check if email already registered (password user)
        user = await _get_user_by_email(db, google_info.email)

        if user is not None:
            # Link Google account to existing password-based user
            user.google_id = google_info.google_id
            logger.info(
                "google_account_linked",
                user_id=user.id,
                email=user.email,
            )
        else:
            # 3b. Brand new user — create account
            # Only patient and doctor are allowed via OAuth — never admin.
            resolved_role = UserRole(role) if role in _ALLOWED_OAUTH_ROLES else UserRole.PATIENT
            is_new_doctor = resolved_role == UserRole.DOCTOR
            user = User(
                email=google_info.email,
                full_name=google_info.full_name,
                role=resolved_role,
                google_id=google_info.google_id,
                is_active=not is_new_doctor,  # Doctors start inactive — require admin approval
                is_verified=True,             # Google already verified the email
            )
            db.add(user)
            await db.flush()

            # Create role-specific profile
            if resolved_role == UserRole.PATIENT:
                patient_code = await _generate_unique_patient_code(db)
                db.add(PatientProfile(user_id=user.id, patient_code=patient_code))
            else:
                db.add(DoctorProfile(user_id=user.id, notifications_enabled=True))
                # Notify admins — same as normal doctor registration
                try:
                    from src.workers.tasks.notifications import notify_admins_doctor_registered
                    notify_admins_doctor_registered.delay(
                        doctor_name=user.full_name,
                        doctor_id=user.id,
                    )
                except Exception as exc:
                    logger.warning("notify_admins_doctor_registered_enqueue_failed_oauth", error=str(exc))

            logger.info(
                "google_user_registered",
                user_id=user.id,
                email=user.email,
                role=resolved_role.value,
            )

    if not user.is_active:
        if user.role == UserRole.DOCTOR:
            # Commit the new account before raising so it isn't rolled back.
            # (Session rolls back on exception — committing first preserves the row.)
            await db.commit()
            raise DoctorPendingApprovalException()
        raise UnauthorizedException(message="Account is suspended")

    # Always refresh fcm_token on Google sign-in
    if fcm_token:
        user.fcm_token = fcm_token

    # 4. Issue tokens
    raw_refresh = generate_refresh_token()
    await _create_refresh_token_row(db, user.id, raw_refresh)

    logger.info("google_auth_success", user_id=user.id)

    return _build_token_response(user, raw_refresh)


# ------------------------------------------------------------------ #
# OTP helpers
# ------------------------------------------------------------------ #

def _generate_otp() -> str:
    """Return a cryptographically secure 6-digit OTP string."""
    import secrets
    return f"{secrets.randbelow(1_000_000):06d}"


async def _create_verification_token(
    db: AsyncSession,
    user_id: str,
    purpose: TokenPurpose,
    expiry_minutes: int,
) -> str:
    """
    Generate a 6-digit OTP, persist its hash, and return the raw code.

    Any previous unused OTPs for this user+purpose are invalidated first —
    this ensures only the latest OTP is valid and avoids hash collisions
    from duplicate codes sitting in the table.
    """
    # Invalidate any outstanding OTPs for this user+purpose
    existing = await db.execute(
        select(VerificationToken).where(
            VerificationToken.user_id == user_id,
            VerificationToken.purpose == purpose,
            VerificationToken.used == False,  # noqa: E712
        )
    )
    now = datetime.now(tz=timezone.utc)
    for row in existing.scalars():
        row.used = True
        row.used_at = now
    await db.flush()

    otp = _generate_otp()
    token_row = VerificationToken(
        user_id=user_id,
        token_hash=hash_refresh_token(otp),
        purpose=purpose,
        expires_at=now + timedelta(minutes=expiry_minutes),
        used=False,
    )
    db.add(token_row)
    await db.flush()
    return otp


async def _redeem_otp(
    db: AsyncSession,
    user_id: str,
    otp: str,
    purpose: TokenPurpose,
) -> VerificationToken:
    """
    Look up and validate an OTP for a specific user. Marks it as used.

    Scoped to user_id so a valid OTP for user A cannot redeem user B's token.

    Raises InvalidTokenException for not-found, already-used, or expired OTPs.
    """
    otp_hash = hash_refresh_token(otp)
    result = await db.execute(
        select(VerificationToken).where(
            VerificationToken.user_id == user_id,
            VerificationToken.token_hash == otp_hash,
            VerificationToken.purpose == purpose,
            VerificationToken.used == False,  # noqa: E712
        )
    )
    token_row = result.scalar_one_or_none()

    if token_row is None:
        raise InvalidTokenException(message="OTP is invalid or has already been used")

    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(tz=timezone.utc):
        raise InvalidTokenException(message="OTP has expired")

    token_row.used = True
    token_row.used_at = datetime.now(tz=timezone.utc)
    await db.flush()
    return token_row


# ------------------------------------------------------------------ #
# Forgot Password
# ------------------------------------------------------------------ #

async def forgot_password(db: AsyncSession, email: str) -> None:
    """
    Generate a 6-digit OTP and email it to the user for password reset.

    Deliberately succeeds silently even when the email is not registered —
    this prevents user enumeration attacks (attacker cannot tell if the
    email exists from the response).

    Google-only accounts (no password_hash) receive a friendly redirect message.
    """
    from src.core.email import render_password_reset_otp_email, send_email_async

    user = await _get_user_by_email(db, email)
    if user is None:
        # Silent no-op — don't leak whether email exists
        logger.info("forgot_password_email_not_found", email=email)
        return
    if not user.is_active and user.role != UserRole.DOCTOR:
        # Suspended non-doctor accounts: silent no-op
        logger.info("forgot_password_inactive_user", email=email)
        return

    if not user.password_hash:
        # Google-only account — send a friendly redirect message
        await send_email_async(
            to_email=email,
            subject="AiDerm Cliniq — Password Reset",
            html_body=(
                f"<p>Hi {user.full_name},</p>"
                "<p>Your account uses Google Sign-In and does not have a password. "
                "Please use the <b>Sign in with Google</b> button in the app.</p>"
            ),
        )
        return

    otp = await _create_verification_token(
        db, user.id, TokenPurpose.PASSWORD_RESET,
        expiry_minutes=_PASSWORD_RESET_EXPIRY_MINUTES,
    )
    html = render_password_reset_otp_email(name=user.full_name, otp=otp)
    await send_email_async(
        to_email=email,
        subject="AiDerm Cliniq — Password Reset OTP",
        html_body=html,
    )
    logger.info("password_reset_otp_sent", user_id=user.id)


# ------------------------------------------------------------------ #
# Reset Password
# ------------------------------------------------------------------ #

async def reset_password(db: AsyncSession, email: str, otp: str, new_password: str) -> None:
    """
    Validate the OTP and update the user's password.

    Raises:
        InvalidTokenException — OTP expired, already used, or not found
    """
    user = await _get_user_by_email(db, email)
    if user is None or not user.is_active:
        # Use same error as invalid OTP to prevent user enumeration
        raise InvalidTokenException(message="OTP is invalid or has expired")

    await _redeem_otp(db, user.id, otp, TokenPurpose.PASSWORD_RESET)
    user.password_hash = await asyncio.to_thread(hash_password, new_password)
    logger.info("password_reset_complete", user_id=user.id)


# ------------------------------------------------------------------ #
# Change Password (logged-in user)
# ------------------------------------------------------------------ #

async def change_password(
    _db: AsyncSession,
    user: User,
    current_password: str,
    new_password: str,
) -> None:
    """
    Change password for a logged-in user who knows their current password.

    Raises:
        BadRequestException     — current_password is wrong
        BadRequestException     — Google-only account (no password to change)
        BadRequestException     — new password same as current
    """
    if not user.password_hash:
        raise BadRequestException(
            message="Your account uses Google Sign-In and does not have a password. "
                    "Use 'Sign in with Google' to access your account."
        )

    is_valid = await asyncio.to_thread(verify_password, current_password, user.password_hash)
    if not is_valid:
        raise BadRequestException(message="Current password is incorrect")

    is_same = await asyncio.to_thread(verify_password, new_password, user.password_hash)
    if is_same:
        raise BadRequestException(message="New password must be different from the current password")

    user.password_hash = await asyncio.to_thread(hash_password, new_password)
    logger.info("password_changed", user_id=user.id)


# ------------------------------------------------------------------ #
# Email Verification
# ------------------------------------------------------------------ #

async def request_email_verification(db: AsyncSession, user: User) -> None:
    """
    Send (or resend) a 6-digit OTP to the user's email for verification.

    No-op if the user is already verified.
    """
    from src.core.email import render_email_verify_otp_email, send_email_async

    if user.is_verified:
        logger.info("email_already_verified", user_id=user.id)
        return

    otp = await _create_verification_token(
        db, user.id, TokenPurpose.EMAIL_VERIFY,
        expiry_minutes=_EMAIL_VERIFY_EXPIRY_MINUTES,
    )
    html = render_email_verify_otp_email(name=user.full_name, otp=otp)
    await send_email_async(
        to_email=user.email,
        subject="AiDerm Cliniq — Verify Your Email",
        html_body=html,
    )
    logger.info("email_verification_otp_sent", user_id=user.id)


async def verify_email(db: AsyncSession, user: User, otp: str) -> None:
    """
    Confirm email ownership using a 6-digit OTP and mark the user as verified.

    Raises:
        BadRequestException   — user already verified
        InvalidTokenException — OTP expired, already used, or not found
    """
    if user.is_verified:
        raise BadRequestException(message="Email address is already verified")

    await _redeem_otp(db, user.id, otp, TokenPurpose.EMAIL_VERIFY)
    user.is_verified = True
    logger.info("email_verified", user_id=user.id)
