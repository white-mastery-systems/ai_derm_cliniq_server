"""
users/schemas.py — User Profile Request & Response Models
==========================================================

WHY SEPARATE FROM auth/schemas.py?
-------------------------------------
auth/schemas.py owns the shapes used DURING authentication (register, login,
token responses). Once a user is logged in, they interact with their profile
through these schemas — the concerns are different.

PROFILE UPDATE DESIGN
----------------------
A single ProfileUpdateRequest handles updates for both patients and doctors.
The service determines which fields to actually apply based on the user's role.
Sending doctor-only fields as a patient is silently ignored — no error.
This keeps the client simple: one request shape, one endpoint.

ALL FIELDS OPTIONAL
--------------------
PATCH semantics: only send what you want to change.
A field not present in the JSON body → not touched in the DB.
We use None as the "not provided" sentinel with model_config exclude_unset.
"""

from datetime import date
from pydantic import BaseModel, Field


# ================================================================== #
# Profile Response — what GET /me returns
# ================================================================== #

class PatientProfileOut(BaseModel):
    """Patient-specific profile fields embedded in UserProfileResponse."""
    patient_code: str
    date_of_birth: date | None = None
    gender: str | None = None
    phone: str | None = None
    avatar_url: str | None = None

    model_config = {"from_attributes": True}


class DoctorProfileOut(BaseModel):
    """Doctor-specific profile fields embedded in UserProfileResponse."""
    specialization: str | None = None
    license_number: str | None = None
    clinic_name: str | None = None
    notifications_enabled: bool = True
    avatar_url: str | None = None

    model_config = {"from_attributes": True}


class UserProfileResponse(BaseModel):
    """
    Full profile returned by GET /api/v1/users/me.

    The `profile` field contains role-specific data:
    - role == "patient" → PatientProfileOut
    - role == "doctor"  → DoctorProfileOut
    - role == "admin"   → None (admins have no extended profile)
    """
    id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    is_verified: bool
    patient_profile: PatientProfileOut | None = None
    doctor_profile: DoctorProfileOut | None = None

    model_config = {"from_attributes": True}


# ================================================================== #
# Profile Update — PATCH /me body
# ================================================================== #

class ProfileUpdateRequest(BaseModel):
    """
    PATCH /api/v1/users/me

    All fields are optional — send only what needs changing.

    Shared fields (patient + doctor):
        full_name    — display name
        phone        — contact number

    Patient-only fields:
        date_of_birth, gender

    Doctor-only fields:
        specialization, license_number, clinic_name, notifications_enabled

    Fields not relevant to the user's role are silently ignored.
    Avatar upload is handled separately via POST /users/me/avatar (Layer 8 GCS).
    """
    # Shared
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    phone: str | None = Field(default=None, max_length=30)

    # Patient-only
    date_of_birth: date | None = None
    gender: str | None = Field(
        default=None,
        max_length=50,
        description="Male | Female | Other | Prefer not to say",
    )

    # Doctor-only
    specialization: str | None = Field(default=None, max_length=255)
    license_number: str | None = Field(default=None, max_length=100)
    clinic_name: str | None = Field(default=None, max_length=255)
    notifications_enabled: bool | None = None

    def has_updates(self) -> bool:
        """Return True if at least one field was provided."""
        return any(v is not None for v in self.model_dump().values())

    model_config = {"from_attributes": True}


# ================================================================== #
# Patient Code Lookup — GET /users/by-code/{patient_code}
# ================================================================== #

class PatientByCodeResponse(BaseModel):
    """
    Returned when a doctor looks up a patient by their patient code.
    Only exposes fields safe for a doctor to see before being assigned to a case.
    """
    user_id: str
    full_name: str
    patient_code: str
    date_of_birth: date | None = None
    gender: str | None = None
    avatar_url: str | None = None

    model_config = {"from_attributes": True}


# ================================================================== #
# Avatar Upload — POST /users/me/avatar
# ================================================================== #

class AvatarUploadResponse(BaseModel):
    """Returned after a successful avatar upload."""
    avatar_url: str
    message: str = "Avatar uploaded successfully"


# ================================================================== #
# Device Token — POST /users/me/device-token
# ================================================================== #

class DeviceTokenRequest(BaseModel):
    """Body for POST /api/v1/users/me/device-token."""
    fcm_token: str = Field(
        max_length=512,
        description="Firebase Cloud Messaging device token from the Flutter app",
    )


class DeviceTokenResponse(BaseModel):
    message: str = "Device token updated"
