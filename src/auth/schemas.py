"""
auth/schemas.py — Auth Request & Response Models
=================================================

Pydantic v2 models for all auth endpoint inputs and outputs.

WHY SEPARATE SCHEMAS FROM MODELS?
-----------------------------------
SQLAlchemy models (src/models/) are DB representations — they have
DB-specific types, relationships, and lazy-loading behaviour.

Pydantic schemas are API representations — they define what JSON
the client sends and receives. Keeping them separate means:
- You control exactly what data is exposed (e.g. never expose password_hash)
- You can have different shapes for request vs response
- DB model changes don't automatically break the API contract

NAMING CONVENTION
-----------------
<Resource>Request  → what the client sends (POST body)
<Resource>Response → what the server returns
<Resource>In       → alternative name for requests (both are used)
"""

from datetime import date

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


# ================================================================== #
# Registration
# ================================================================== #

class PatientRegisterRequest(BaseModel):
    """
    POST /api/v1/auth/register/patient

    Creates a patient account. DOB and gender are collected upfront so the
    AI can use them immediately for complaint suggestions and diagnosis context.
    Phone and avatar can be updated later via PATCH /users/me.
    """
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(
        min_length=8,
        max_length=128,
        description="Min 8 characters",
    )
    date_of_birth: date | None = Field(
        default=None,
        description="Patient's date of birth — used to compute age for AI analysis",
    )
    gender: str | None = Field(
        default=None,
        max_length=50,
        description="Male | Female | Other | Prefer not to say",
    )
    fcm_token: str | None = Field(
        default=None,
        max_length=512,
        description="Firebase Cloud Messaging device token for push notifications",
    )

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        """Ensure password has at least one digit and one letter."""
        has_letter = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_letter and has_digit):
            raise ValueError("Password must contain at least one letter and one digit")
        return v


class DoctorRegisterRequest(BaseModel):
    """
    POST /api/v1/auth/register/doctor

    Doctors additionally provide specialization and license.
    These can be updated later via PATCH /users/me.
    """
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    specialization: str | None = Field(default=None, max_length=255)
    license_number: str | None = Field(default=None, max_length=100)
    clinic_name: str | None = Field(default=None, max_length=255)
    fcm_token: str | None = Field(
        default=None,
        max_length=512,
        description="Firebase Cloud Messaging device token for push notifications",
    )

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        has_letter = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_letter and has_digit):
            raise ValueError("Password must contain at least one letter and one digit")
        return v


# ================================================================== #
# Login
# ================================================================== #

class LoginRequest(BaseModel):
    """
    POST /api/v1/auth/login

    Single login endpoint for all roles.
    The role is read from the user's DB record — not trusted from the client.
    """
    email: EmailStr
    password: str
    fcm_token: str | None = Field(
        default=None,
        max_length=512,
        description="Firebase Cloud Messaging device token — updated on every login",
    )


# ================================================================== #
# Token Responses
# ================================================================== #

class TokenResponse(BaseModel):
    """
    Returned after successful login or token refresh.

    access_token:  Short-lived (60 min). Sent in Authorization header.
    refresh_token: Long-lived (30 days). Sent only to /auth/refresh.
    token_type:    Always "bearer" — Flutter adds "Bearer {access_token}".
    """
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str
    user_id: str


class RefreshRequest(BaseModel):
    """POST /api/v1/auth/refresh"""
    refresh_token: str


# ================================================================== #
# Logout
# ================================================================== #

class LogoutRequest(BaseModel):
    """
    POST /api/v1/auth/logout

    Client sends its refresh token so we can revoke it server-side.
    This prevents the 30-day window attack if the token is stolen.
    """
    refresh_token: str


# ================================================================== #
# Google OAuth
# ================================================================== #

class GoogleAuthRequest(BaseModel):
    """
    POST /api/v1/auth/google

    Mobile (Android/iOS) sends id_token from the Google Sign-In SDK.
    Flutter web sends access_token — the GIS OAuth popup flow does not return
    an id_token on web. Exactly one of the two must be provided.

    role is only used when creating a new account on first login.
    """
    id_token: str | None = Field(
        default=None,
        description="Google ID token — returned by native mobile Google Sign-In",
    )
    access_token: str | None = Field(
        default=None,
        description="Google access token — Flutter web fallback when idToken is null",
    )
    role: str = Field(
        default="patient",
        pattern="^(patient|doctor)$",
        description="patient or doctor — used only on first login",
    )
    fcm_token: str | None = Field(
        default=None,
        max_length=512,
        description="Firebase Cloud Messaging device token for push notifications",
    )

    @model_validator(mode="after")
    def require_one_token(self) -> "GoogleAuthRequest":
        if not self.id_token and not self.access_token:
            raise ValueError("Either id_token or access_token must be provided")
        return self


# ================================================================== #
# User Info Response
# ================================================================== #

class UserResponse(BaseModel):
    """
    Returned after registration or in GET /users/me.
    Never includes password_hash or internal token fields.
    """
    id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    is_verified: bool
    patient_code: str | None = None  # Only for patients

    model_config = {"from_attributes": True}


class RegisterResponse(BaseModel):
    """
    POST /api/v1/auth/register/patient response.
    Includes tokens so the patient is logged in immediately after registration.
    """
    user: UserResponse
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    """Generic success message response used by several auth endpoints."""
    message: str


class ForgotPasswordRequest(BaseModel):
    """POST /api/v1/auth/forgot-password"""
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """POST /api/v1/auth/reset-password"""
    email: EmailStr = Field(description="The email address the OTP was sent to")
    otp: str = Field(
        min_length=6,
        max_length=6,
        pattern=r"^\d{6}$",
        description="6-digit OTP received in the password-reset email",
    )
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        has_letter = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_letter and has_digit):
            raise ValueError("Password must contain at least one letter and one digit")
        return v


class VerifyEmailOTPRequest(BaseModel):
    """POST /api/v1/auth/verify-email/confirm"""
    otp: str = Field(
        min_length=6,
        max_length=6,
        pattern=r"^\d{6}$",
        description="6-digit OTP received in the verification email",
    )


class ChangePasswordRequest(BaseModel):
    """POST /api/v1/auth/change-password"""
    current_password: str = Field(min_length=1, description="The user's current password")
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        has_letter = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_letter and has_digit):
            raise ValueError("Password must contain at least one letter and one digit")
        return v


class DoctorRegisterResponse(BaseModel):
    """
    POST /api/v1/auth/register/doctor response.

    Doctors are NOT logged in immediately — email verification is required first,
    then admin approval. No tokens are issued at registration time.
    """
    user: UserResponse
    message: str = (
        "Registration successful. A 6-digit verification code has been sent to your email. "
        "Please verify your email to complete registration. "
        "After verification, your account will be reviewed by an admin before you can log in."
    )


class VerifyDoctorEmailRequest(BaseModel):
    """POST /api/v1/auth/verify-doctor-email"""
    email: EmailStr = Field(description="The email address used during registration")
    otp: str = Field(
        min_length=6,
        max_length=6,
        pattern=r"^\d{6}$",
        description="6-digit OTP received in the registration verification email",
    )


class ResendDoctorVerificationRequest(BaseModel):
    """POST /api/v1/auth/resend-doctor-verification"""
    email: EmailStr = Field(description="The email address used during registration")
