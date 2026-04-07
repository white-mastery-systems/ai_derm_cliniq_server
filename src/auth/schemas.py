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

from pydantic import BaseModel, EmailStr, Field, field_validator


# ================================================================== #
# Registration
# ================================================================== #

class PatientRegisterRequest(BaseModel):
    """
    POST /api/v1/auth/register/patient

    Minimum required to create a patient account.
    Profile fields (DOB, phone, gender) are set via PATCH /users/me later.
    """
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(
        min_length=8,
        max_length=128,
        description="Min 8 characters",
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

    Flutter uses Google Sign-In SDK → receives an ID token.
    This token is sent here for server-side verification.

    role is required so we know whether to create a patient or doctor profile
    if this is the user's first Google login.
    """
    id_token: str
    role: str = Field(
        default="patient",
        pattern="^(patient|doctor)$",
        description="patient or doctor — used only on first login",
    )


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


class DoctorRegisterResponse(BaseModel):
    """
    POST /api/v1/auth/register/doctor response.

    Doctors are NOT logged in immediately — they must wait for admin approval.
    No tokens are issued. The Flutter app should show a "pending approval" screen.
    """
    user: UserResponse
    message: str = "Registration successful. Your account is pending admin approval. You will be able to log in once an admin verifies your account."
