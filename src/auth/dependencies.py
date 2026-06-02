"""
auth/dependencies.py — FastAPI Auth Dependencies
=================================================

FastAPI dependencies are functions injected into route handlers via `Depends()`.
Auth dependencies do two things:
1. Extract and validate the JWT from the request header (authentication)
2. Check the user's role against what the route requires (authorization)

HOW FASTAPI DEPENDENCIES WORK
-------------------------------
When a route declares:
    async def my_route(user: User = Depends(get_current_user)):

FastAPI automatically:
1. Calls get_current_user(request)
2. If it raises an exception → returns that HTTP error
3. If it returns a value → passes it to my_route as `user`

DEPENDENCY HIERARCHY
---------------------
get_current_user      →  validates JWT, returns User
    └── require_patient  →  user.role must be PATIENT
    └── require_doctor   →  user.role must be DOCTOR
    └── require_admin    →  user.role must be ADMIN
    └── require_roles([...])  →  user.role must be in the list

HEADER FORMAT
--------------
Authorization: Bearer <access_token>

The Bearer scheme is the standard for JWT-based APIs.
FastAPI's OAuth2PasswordBearer extracts the token from the header automatically.

WHY NOT USE OAuth2PasswordBearer?
-----------------------------------
We use a manual extraction approach so we can return our custom error format
(AppException → {"error": {...}}) instead of FastAPI's default 401.
OAuth2PasswordBearer returns a generic WWW-Authenticate header that doesn't
match our API contract.
"""

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.jwt import decode_access_token
from src.database.core import get_async_session
from src.exceptions import (
    DoctorPendingApprovalException,
    ForbiddenException,
    InsufficientRoleException,
    InvalidTokenException,
    UnauthorizedException,
)
from src.models.user import User, UserRole


# ------------------------------------------------------------------ #
# Token Extraction
# ------------------------------------------------------------------ #

def _extract_bearer_token(request: Request) -> str:
    """
    Extract the JWT from the Authorization: Bearer <token> header.

    Raises UnauthorizedException if the header is missing or malformed.
    This is the first gate — no token, no entry.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise UnauthorizedException(
            message="Authorization header missing or not Bearer scheme"
        )
    token = auth_header[len("Bearer "):]
    if not token:
        raise UnauthorizedException(message="Bearer token is empty")
    return token


# ------------------------------------------------------------------ #
# Core Dependency — Authentication
# ------------------------------------------------------------------ #

async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """
    FastAPI dependency: validate JWT and return the authenticated User.

    WHAT IT DOES
    ------------
    1. Extracts token from Authorization header
    2. Decodes + validates JWT (signature, expiry, type = "access")
    3. Looks up the user in the DB (confirms account still exists + is active)
    4. Returns the User ORM object

    WHY DB LOOKUP ON EVERY REQUEST?
    ---------------------------------
    The JWT contains user_id and role. We could skip the DB lookup and
    trust the JWT claims alone — this is truly stateless.

    But we do a lightweight DB lookup for one reason:
    If an admin suspends a user (is_active = False), the user's existing
    tokens would still work until they expire (up to 60 min).

    The DB lookup catches this immediately. It's one indexed PK lookup —
    fast and worth the safety guarantee.

    Raises:
        UnauthorizedException  — missing/malformed header
        InvalidTokenException  — bad signature, expired, wrong type
        UnauthorizedException  — user not found or account suspended
    """
    token = _extract_bearer_token(request)
    payload = decode_access_token(token)  # raises InvalidTokenException if bad

    user_id: str | None = payload.get("sub")
    if not user_id:
        raise InvalidTokenException(message="Token missing subject claim")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise UnauthorizedException(message="User account not found")

    if not user.is_active:
        raise UnauthorizedException(message="Account is suspended")

    return user


# ------------------------------------------------------------------ #
# Role Guards — Authorization
# ------------------------------------------------------------------ #

async def require_patient(user: User = Depends(get_current_user)) -> User:
    """
    Dependency: route is only accessible to PATIENT-role users.

    Admins are also permitted so they can test the patient flow without
    needing a separate patient account. Matches the same bypass pattern
    used in require_doctor.

    Usage:
        @router.get("/my-cases")
        async def get_my_cases(patient: User = Depends(require_patient)):
            ...
    """
    if user.role == UserRole.ADMIN:
        return user  # admins can access patient endpoints for testing
    if user.role != UserRole.PATIENT:
        raise InsufficientRoleException(
            message="This endpoint requires the 'patient' role"
        )
    return user


async def require_doctor(user: User = Depends(get_current_user)) -> User:
    """
    Dependency: route is accessible to DOCTOR-role users and ADMIN-role users.

    Admins are promoted from doctors and retain full doctor privileges —
    they can review cases, scan QR codes, and use all doctor endpoints.

    Unverified doctors receive 403 DOCTOR_PENDING_APPROVAL.
    Patients and other roles receive 403 INSUFFICIENT_ROLE.

    Usage:
        @router.post("/cases/{case_id}/review")
        async def submit_review(doctor: User = Depends(require_doctor)):
            ...
    """
    if user.role == UserRole.ADMIN:
        return user  # admins always pass doctor checks
    if user.role != UserRole.DOCTOR:
        raise InsufficientRoleException(
            message="This endpoint requires the 'doctor' role"
        )
    if not user.is_verified:
        raise DoctorPendingApprovalException()
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """
    Dependency: route is only accessible to ADMIN-role users.

    Usage:
        @router.delete("/users/{user_id}")
        async def delete_user(admin: User = Depends(require_admin)):
            ...
    """
    if user.role != UserRole.ADMIN:
        raise InsufficientRoleException(
            message="This endpoint requires the 'admin' role"
        )
    return user


async def require_patient_or_assigned_doctor(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """
    Dependency: route is accessible to:
    - Any PATIENT (ownership is enforced in the service layer)
    - A verified DOCTOR who is the assigned doctor for the case

    Use on endpoints that patients initiate but doctors can also action
    when they have been assigned to the case (e.g. upload image, complaint
    suggestions, assessment depth, red flag check).
    """
    if user.role == UserRole.PATIENT:
        return user
    if user.role == UserRole.ADMIN:
        return user  # admin has full access — no assignment check needed
    if user.role == UserRole.DOCTOR:
        if not user.is_verified:
            raise DoctorPendingApprovalException()
        from src.models.case import Case
        result = await db.execute(select(Case).where(Case.id == case_id))
        case = result.scalar_one_or_none()
        if case is not None and case.doctor_id == user.id:
            return user
        raise ForbiddenException(message="You are not the assigned doctor for this case")
    raise InsufficientRoleException(
        message="This endpoint requires the 'patient' role or being the assigned doctor"
    )


def require_roles(allowed_roles: list[UserRole]):
    """
    Dependency factory: route is accessible to any of the specified roles.

    Use this when multiple roles share an endpoint.

    Usage:
        @router.get("/cases/{case_id}")
        async def get_case(
            user: User = Depends(require_roles([UserRole.PATIENT, UserRole.DOCTOR]))
        ):
            ...
    """
    async def _check_role(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles:
            role_names = ", ".join(r.value for r in allowed_roles)
            raise InsufficientRoleException(
                message=f"This endpoint requires one of: {role_names}"
            )
        return user
    return _check_role
