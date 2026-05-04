"""
auth/controller.py — Auth HTTP Endpoints
==========================================

All routes are prefixed with /api/v1/auth (set in src/api.py).

ROUTES
------
POST /register/patient   →  create patient account, return tokens
POST /register/doctor    →  create doctor account, return tokens
POST /login              →  email + password login, return tokens
POST /refresh            →  rotate refresh token pair
POST /logout             →  revoke refresh token
POST /google             →  Google OAuth sign-in / register

DESIGN PRINCIPLES
-----------------
Controllers are thin. They only:
1. Parse and validate the request body (Pydantic does this automatically)
2. Call the appropriate service function
3. Build and return the HTTP response

All business logic lives in service.py.
All token and security logic lives in jwt.py / security.py.

STATUS CODES
------------
201 Created  →  registration (a new resource was created)
200 OK       →  login, refresh, google (authentication — no new resource)
204 No Content → logout (success, nothing to return)
"""

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import service
from src.auth.schemas import (
    ChangePasswordRequest,
    DoctorRegisterRequest,
    DoctorRegisterResponse,
    ForgotPasswordRequest,
    GoogleAuthRequest,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    PatientRegisterRequest,
    RefreshRequest,
    RegisterResponse,
    ResetPasswordRequest,
    TokenResponse,
    UserResponse,
    VerifyEmailOTPRequest,
)
from src.auth.dependencies import get_current_user
from src.database.core import get_async_session
from src.models.user import User
from src.rate_limiting import limiter

router = APIRouter()


# ------------------------------------------------------------------ #
# Registration
# ------------------------------------------------------------------ #

@router.post(
    "/register/patient",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new patient account",
    description=(
        "Creates a patient account and returns auth tokens so the user is "
        "immediately logged in. Profile fields (DOB, phone, gender) can be "
        "updated later via PATCH /users/me."
    ),
)
@limiter.limit("5/minute")
async def register_patient(
    request: Request,  # noqa: ARG001 — required by slowapi for rate limiting
    body: PatientRegisterRequest,
    db: AsyncSession = Depends(get_async_session),
) -> RegisterResponse:
    user, tokens, patient_code = await service.register_patient(db, body)
    return RegisterResponse(
        user=UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role.value,
            is_active=user.is_active,
            is_verified=user.is_verified,
            patient_code=patient_code,
        ),
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        token_type=tokens.token_type,
    )


@router.post(
    "/register/doctor",
    response_model=DoctorRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new doctor account",
    description=(
        "Submits a doctor registration request. "
        "The account is created in a pending state — no tokens are issued. "
        "An admin must approve the account via PATCH /api/v1/admin/users/{user_id} "
        "before the doctor can log in."
    ),
)
@limiter.limit("5/minute")
async def register_doctor(
    request: Request,  # noqa: ARG001 — required by slowapi for rate limiting
    body: DoctorRegisterRequest,
    db: AsyncSession = Depends(get_async_session),
) -> DoctorRegisterResponse:
    user = await service.register_doctor(db, body)
    return DoctorRegisterResponse(
        user=UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role.value,
            is_active=user.is_active,
            is_verified=user.is_verified,
        ),
    )


# ------------------------------------------------------------------ #
# Login
# ------------------------------------------------------------------ #

@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Login with email and password",
    description=(
        "Authenticates a user and returns an access token (60 min) and "
        "refresh token (30 days). Works for all roles (patient/doctor/admin)."
    ),
)
@limiter.limit("10/minute")
async def login(
    request: Request,  # noqa: ARG001 — required by slowapi for rate limiting  # noqa: ARG001
    body: LoginRequest,
    db: AsyncSession = Depends(get_async_session),
) -> TokenResponse:
    return await service.login(db, body)


# ------------------------------------------------------------------ #
# Token Refresh
# ------------------------------------------------------------------ #

@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Refresh access token",
    description=(
        "Exchange a valid refresh token for a new access + refresh token pair. "
        "The old refresh token is immediately revoked (token rotation)."
    ),
)
async def refresh_tokens(
    request: RefreshRequest,
    db: AsyncSession = Depends(get_async_session),
) -> TokenResponse:
    return await service.refresh_tokens(db, request.refresh_token)


# ------------------------------------------------------------------ #
# Logout
# ------------------------------------------------------------------ #

@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Logout — revoke refresh token",
    description=(
        "Revokes the given refresh token server-side. "
        "The client must also discard the access token locally. "
        "Returns 204 with no body on success."
    ),
)
async def logout(
    request: LogoutRequest,
    db: AsyncSession = Depends(get_async_session),
) -> None:
    await service.logout(db, request.refresh_token)


# ------------------------------------------------------------------ #
# Google OAuth
# ------------------------------------------------------------------ #

@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Request a password-reset email",
    description=(
        "Sends a password-reset link to the given email address. "
        "Always returns 200 regardless of whether the email exists — "
        "this prevents user enumeration. The link expires in 15 minutes."
    ),
)
@limiter.limit("5/minute")
async def forgot_password(
    request: Request,  # noqa: ARG001
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    await service.forgot_password(db, body.email)
    return MessageResponse(
        message="If that email is registered, a reset link has been sent."
    )


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Set a new password using the OTP from the reset email",
    description=(
        "Validates the 6-digit OTP sent to the user's email and updates the password. "
        "The OTP expires in 15 minutes and is single-use."
    ),
)
@limiter.limit("5/minute")
async def reset_password(
    request: Request,  # noqa: ARG001
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    await service.reset_password(db, body.email, body.otp, body.new_password)
    return MessageResponse(message="Password updated successfully. You can now log in.")


@router.post(
    "/verify-email",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Resend the email verification link",
    description="Requires a valid access token. No-op if already verified.",
)
async def resend_email_verification(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    await service.request_email_verification(db, current_user)
    return MessageResponse(message="Verification email sent. Check your inbox.")


@router.post(
    "/verify-email/confirm",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm email address using the OTP from the verification email",
    description=(
        "Requires a valid access token. "
        "Validates the 6-digit OTP sent to the user's inbox and marks the account as verified. "
        "The OTP expires in 30 minutes and is single-use."
    ),
)
@limiter.limit("5/minute")
async def verify_email(
    request: Request,  # noqa: ARG001
    body: VerifyEmailOTPRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    await service.verify_email(db, current_user, body.otp)
    return MessageResponse(message="Email verified successfully.")


@router.post(
    "/change-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Change password (logged-in user)",
    description=(
        "Allows a logged-in user to change their password by providing their "
        "current password and a new one. Requires a valid access token. "
        "Not applicable to Google-only accounts."
    ),
)
async def change_password(
    body: ChangePasswordRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    await service.change_password(db, current_user, body.current_password, body.new_password)
    return MessageResponse(message="Password changed successfully.")


@router.post(
    "/google",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign in / register with Google",
    description=(
        "Verifies a Google ID token from the Flutter Google Sign-In SDK. "
        "Creates a new account on first login, or signs in an existing user. "
        "The 'role' field is only used when creating a new account."
    ),
)
async def google_auth(
    request: GoogleAuthRequest,
    db: AsyncSession = Depends(get_async_session),
) -> TokenResponse:
    return await service.google_auth(db, request.id_token, request.role, request.fcm_token)
