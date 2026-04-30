"""
tests/integration/test_admin_endpoints.py — Admin Endpoint Integration Tests
=============================================================================

Tests for:
    GET    /api/v1/admin/users                       — list users (filtered)
    GET    /api/v1/admin/users/{user_id}             — get user detail
    PATCH  /api/v1/admin/users/{user_id}             — update account state
    DELETE /api/v1/admin/users/{user_id}             — permanently delete user
    POST   /api/v1/admin/users/{user_id}/resend-verification — resend OTP
    GET    /api/v1/admin/cases                       — list all cases
    GET    /api/v1/admin/cases/{case_id}             — case detail
    GET    /api/v1/admin/stats                       — platform statistics
    GET    /api/v1/admin/doctors                     — list all doctors
    GET    /api/v1/admin/doctors/pending             — pending approval doctors
    POST   /api/v1/admin/doctors/{user_id}/approve   — approve doctor
    POST   /api/v1/admin/doctors/{user_id}/reject    — reject and delete doctor
    GET    /api/v1/admin/prompts                     — list all prompt overrides
    PATCH  /api/v1/admin/prompts/{key}               — set prompt override
    DELETE /api/v1/admin/prompts/{key}               — reset prompt to default

ADMIN USER SETUP
----------------
There is no POST /auth/register/admin endpoint — admins are created
via direct DB insertion. Each test module has a `create_admin` helper
that inserts a User row (role=admin) directly using test_engine,
then logs in via POST /auth/login to obtain a JWT.
"""

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.security import hash_password
from src.models.base import new_uuid
from src.models.user import User, UserRole


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def create_admin_user(test_engine, suffix: str) -> tuple[str, str]:
    """Insert an admin user directly into the test DB. Returns (email, password)."""
    email = f"admin_{suffix}@admintest.com"
    password = "AdminPass9"
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        user = User(
            id=new_uuid(),
            email=email,
            full_name=f"Admin {suffix}",
            role=UserRole.ADMIN,
            password_hash=hash_password(password),
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.commit()
    return email, password


async def login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def register_and_login_patient(client: AsyncClient, suffix: str) -> tuple[str, str]:
    await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"Admin Patient {suffix}",
        "email": f"adm_patient_{suffix}@admtest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"adm_patient_{suffix}@admtest.com",
        "password": "TestPass1",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def register_doctor(client: AsyncClient, suffix: str) -> str:
    """Register a doctor (pending approval). Returns the user_id.

    Doctors cannot log in until an admin approves them, so no token is returned.
    Use the admin approve endpoint to unblock login if a test needs a doctor token.
    """
    resp = await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Admin Doctor {suffix}",
        "email": f"adm_doctor_{suffix}@admtest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-ADM-{suffix}",
        "clinic_name": "Admin Clinic",
    })
    return resp.json()["user"]["id"]


async def register_and_login_doctor(client: AsyncClient, suffix: str) -> tuple[None, str]:
    """Compatibility shim — returns (None, doctor_id). Token is None because
    doctors must wait for admin approval before they can log in."""
    doctor_id = await register_doctor(client, suffix)
    return None, doctor_id


# ================================================================== #
# GET /api/v1/admin/users
# ================================================================== #

@pytest.mark.asyncio
async def test_list_users_returns_all(db_app_client: AsyncClient, test_engine):
    """Admin can list all users — gets at least the users created in this test."""
    email, password = await create_admin_user(test_engine, "lu1")
    admin_token = await login(db_app_client, email, password)

    # Create a patient + doctor to appear in list
    await register_and_login_patient(db_app_client, "lu1")
    await register_and_login_doctor(db_app_client, "lu1")

    resp = await db_app_client.get(
        "/api/v1/admin/users",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert body["total"] >= 3  # admin + patient + doctor (at minimum)


@pytest.mark.asyncio
async def test_list_users_filter_by_role(db_app_client: AsyncClient, test_engine):
    """Role filter returns only users matching that role."""
    email, password = await create_admin_user(test_engine, "lu2")
    admin_token = await login(db_app_client, email, password)

    await register_and_login_patient(db_app_client, "lu2")

    resp = await db_app_client.get(
        "/api/v1/admin/users?role=patient",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(item["role"] == "patient" for item in body["items"])


@pytest.mark.asyncio
async def test_list_users_filter_invalid_role(db_app_client: AsyncClient, test_engine):
    """Invalid role filter returns 400."""
    email, password = await create_admin_user(test_engine, "lu3")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.get(
        "/api/v1/admin/users?role=superuser",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_users_requires_admin(db_app_client: AsyncClient, test_engine):
    """Non-admin users get 403."""
    patient_token, _ = await register_and_login_patient(db_app_client, "lu4")

    resp = await db_app_client.get(
        "/api/v1/admin/users",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_users_requires_auth(db_app_client: AsyncClient):
    resp = await db_app_client.get("/api/v1/admin/users")
    assert resp.status_code == 401


# ================================================================== #
# GET /api/v1/admin/users/{user_id}
# ================================================================== #

@pytest.mark.asyncio
async def test_get_user_detail_patient(db_app_client: AsyncClient, test_engine):
    """Admin can get full patient detail including patient_profile."""
    email, password = await create_admin_user(test_engine, "gu1")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "gu1")

    resp = await db_app_client.get(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == patient_id
    assert body["role"] == "patient"
    assert body["patient_profile"] is not None
    assert "patient_code" in body["patient_profile"]


@pytest.mark.asyncio
async def test_get_user_detail_doctor(db_app_client: AsyncClient, test_engine):
    """Admin can get full doctor detail including doctor_profile."""
    email, password = await create_admin_user(test_engine, "gu2")
    admin_token = await login(db_app_client, email, password)

    _, doctor_id = await register_and_login_doctor(db_app_client, "gu2")

    resp = await db_app_client.get(
        f"/api/v1/admin/users/{doctor_id}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "doctor"
    assert body["doctor_profile"] is not None
    assert body["doctor_profile"]["specialization"] == "Dermatology"


@pytest.mark.asyncio
async def test_get_user_not_found(db_app_client: AsyncClient, test_engine):
    email, password = await create_admin_user(test_engine, "gu3")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.get(
        "/api/v1/admin/users/nonexistent-user-id",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


# ================================================================== #
# PATCH /api/v1/admin/users/{user_id}
# ================================================================== #

@pytest.mark.asyncio
async def test_suspend_user(db_app_client: AsyncClient, test_engine):
    """Admin can suspend a patient account."""
    email, password = await create_admin_user(test_engine, "uu1")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "uu1")

    resp = await db_app_client.patch(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(admin_token),
        json={"is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_reactivate_user(db_app_client: AsyncClient, test_engine):
    """Admin can reactivate a suspended account."""
    email, password = await create_admin_user(test_engine, "uu2")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "uu2")

    # Suspend first
    await db_app_client.patch(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(admin_token),
        json={"is_active": False},
    )

    # Reactivate
    resp = await db_app_client.patch(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(admin_token),
        json={"is_active": True},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


@pytest.mark.asyncio
async def test_verify_user(db_app_client: AsyncClient, test_engine):
    """Admin can mark a user as email-verified."""
    email, password = await create_admin_user(test_engine, "uu3")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "uu3")

    resp = await db_app_client.patch(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(admin_token),
        json={"is_verified": True},
    )
    assert resp.status_code == 200
    assert resp.json()["is_verified"] is True


@pytest.mark.asyncio
async def test_update_user_invalid_role(db_app_client: AsyncClient, test_engine):
    """Invalid role string returns 400."""
    email, password = await create_admin_user(test_engine, "uu4")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "uu4")

    resp = await db_app_client.patch(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(admin_token),
        json={"role": "superuser"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_user_requires_admin(db_app_client: AsyncClient, test_engine):
    patient_token, patient_id = await register_and_login_patient(db_app_client, "uu5")

    resp = await db_app_client.patch(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(patient_token),
        json={"is_active": False},
    )
    assert resp.status_code == 403


# ================================================================== #
# GET /api/v1/admin/cases
# ================================================================== #

@pytest.mark.asyncio
async def test_list_all_cases(db_app_client: AsyncClient, test_engine):
    """Admin sees all cases including those from other patients."""
    email, password = await create_admin_user(test_engine, "lc1")
    admin_token = await login(db_app_client, email, password)

    patient_token, _ = await register_and_login_patient(db_app_client, "lc1")
    # Profile must be complete (DOB + gender) before creating a case
    await db_app_client.patch(
        "/api/v1/users/me",
        headers=auth_header(patient_token),
        json={"date_of_birth": "1990-01-01", "gender": "male"},
    )
    await db_app_client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "is_for_self": True,
            "presenting_complaint": "Test rash",
            "consent_ai_analysis": True,
        },
    )

    resp = await db_app_client.get(
        "/api/v1/admin/cases",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    # Each item has patient_name
    for item in body["items"]:
        assert "patient_name" in item
        assert "ai_status" in item


@pytest.mark.asyncio
async def test_list_cases_filter_by_ai_status(db_app_client: AsyncClient, test_engine):
    """Admin can filter cases by ai_status."""
    email, password = await create_admin_user(test_engine, "lc2")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.get(
        "/api/v1/admin/cases?ai_status=pending",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(item["ai_status"] == "pending" for item in body["items"])


@pytest.mark.asyncio
async def test_list_cases_invalid_filter_returns_400(db_app_client: AsyncClient, test_engine):
    email, password = await create_admin_user(test_engine, "lc3")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.get(
        "/api/v1/admin/cases?ai_status=unknown_status",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_cases_requires_admin(db_app_client: AsyncClient, test_engine):
    patient_token, _ = await register_and_login_patient(db_app_client, "lc4")

    resp = await db_app_client.get(
        "/api/v1/admin/cases",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 403


# ================================================================== #
# GET /api/v1/admin/stats
# ================================================================== #

@pytest.mark.asyncio
async def test_get_stats_returns_counts(db_app_client: AsyncClient, test_engine):
    """Stats endpoint returns all expected count fields."""
    email, password = await create_admin_user(test_engine, "gs1")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.get(
        "/api/v1/admin/stats",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()

    expected_fields = [
        "total_users", "total_patients", "total_doctors", "total_admins",
        "total_cases", "cases_ai_pending", "cases_ai_processing",
        "cases_ai_completed", "cases_ai_failed", "total_reports",
    ]
    for field in expected_fields:
        assert field in body, f"Missing field: {field}"
        assert isinstance(body[field], int)


@pytest.mark.asyncio
async def test_get_stats_counts_are_consistent(db_app_client: AsyncClient, test_engine):
    """total_users == total_patients + total_doctors + total_admins."""
    email, password = await create_admin_user(test_engine, "gs2")
    admin_token = await login(db_app_client, email, password)

    # Register one patient and one doctor to make sure counts are non-trivial
    await register_and_login_patient(db_app_client, "gs2")
    await register_and_login_doctor(db_app_client, "gs2")

    resp = await db_app_client.get(
        "/api/v1/admin/stats",
        headers=auth_header(admin_token),
    )
    body = resp.json()
    assert body["total_users"] == body["total_patients"] + body["total_doctors"] + body["total_admins"]


@pytest.mark.asyncio
async def test_get_stats_requires_admin(db_app_client: AsyncClient, test_engine):
    patient_token, _ = await register_and_login_patient(db_app_client, "gs3")

    resp = await db_app_client.get(
        "/api/v1/admin/stats",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_stats_requires_auth(db_app_client: AsyncClient):
    resp = await db_app_client.get("/api/v1/admin/stats")
    assert resp.status_code == 401


# ================================================================== #
# GET /api/v1/admin/doctors
# ================================================================== #

@pytest.mark.asyncio
async def test_list_doctors_returns_doctors(db_app_client: AsyncClient, test_engine):
    """Admin sees all doctors including newly registered ones."""
    email, password = await create_admin_user(test_engine, "ld1")
    admin_token = await login(db_app_client, email, password)

    await register_doctor(db_app_client, "ld1")

    resp = await db_app_client.get(
        "/api/v1/admin/doctors",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert body["total"] >= 1
    for item in body["items"]:
        assert "full_name" in item
        assert "is_verified" in item


@pytest.mark.asyncio
async def test_list_doctors_requires_admin(db_app_client: AsyncClient, test_engine):
    patient_token, _ = await register_and_login_patient(db_app_client, "ld2")
    resp = await db_app_client.get(
        "/api/v1/admin/doctors",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 403


# ================================================================== #
# GET /api/v1/admin/doctors/pending
# ================================================================== #

@pytest.mark.asyncio
async def test_list_pending_doctors_only_unapproved(db_app_client: AsyncClient, test_engine):
    """Pending list only returns doctors with is_verified=False."""
    email, password = await create_admin_user(test_engine, "lpd1")
    admin_token = await login(db_app_client, email, password)

    await register_doctor(db_app_client, "lpd1")

    resp = await db_app_client.get(
        "/api/v1/admin/doctors/pending",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    # All returned doctors should be unverified
    for item in body["items"]:
        assert item["is_verified"] is False


# ================================================================== #
# POST /api/v1/admin/doctors/{user_id}/approve
# ================================================================== #

@pytest.mark.asyncio
async def test_approve_doctor_activates_account(db_app_client: AsyncClient, test_engine):
    """Approving a doctor sets is_active=True and is_verified=True."""
    email, password = await create_admin_user(test_engine, "apd1")
    admin_token = await login(db_app_client, email, password)

    doctor_id = await register_doctor(db_app_client, "apd1")

    with patch("src.core.email.send_email", return_value=True):
        resp = await db_app_client.post(
            f"/api/v1/admin/doctors/{doctor_id}/approve",
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is True
    assert body["is_verified"] is True
    assert body["role"] == "doctor"


@pytest.mark.asyncio
async def test_approve_doctor_allows_login(db_app_client: AsyncClient, test_engine):
    """After approval the doctor can log in and get a token."""
    email, password = await create_admin_user(test_engine, "apd2")
    admin_token = await login(db_app_client, email, password)

    doctor_id = await register_doctor(db_app_client, "apd2")

    with patch("src.core.email.send_email", return_value=True):
        await db_app_client.post(
            f"/api/v1/admin/doctors/{doctor_id}/approve",
            headers=auth_header(admin_token),
        )

    login_resp = await db_app_client.post("/api/v1/auth/login", json={
        "email": "adm_doctor_apd2@admtest.com",
        "password": "DocPass9",
    })
    assert login_resp.status_code == 200
    assert "access_token" in login_resp.json()


@pytest.mark.asyncio
async def test_approve_doctor_not_found(db_app_client: AsyncClient, test_engine):
    email, password = await create_admin_user(test_engine, "apd3")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.post(
        "/api/v1/admin/doctors/nonexistent-id/approve",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_approve_non_doctor_returns_400(db_app_client: AsyncClient, test_engine):
    """Approving a patient account returns 400."""
    email, password = await create_admin_user(test_engine, "apd4")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "apd4")

    resp = await db_app_client.post(
        f"/api/v1/admin/doctors/{patient_id}/approve",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


# ================================================================== #
# POST /api/v1/admin/doctors/{user_id}/reject
# ================================================================== #

@pytest.mark.asyncio
async def test_reject_doctor_deletes_account(db_app_client: AsyncClient, test_engine):
    """Rejecting a doctor permanently deletes their account."""
    email, password = await create_admin_user(test_engine, "rjd1")
    admin_token = await login(db_app_client, email, password)

    doctor_id = await register_doctor(db_app_client, "rjd1")

    with patch("src.core.email.send_email", return_value=True):
        resp = await db_app_client.post(
            f"/api/v1/admin/doctors/{doctor_id}/reject",
            headers=auth_header(admin_token),
            json={"reason": "License could not be verified"},
        )

    assert resp.status_code == 204

    # Confirm account is gone
    detail_resp = await db_app_client.get(
        f"/api/v1/admin/users/{doctor_id}",
        headers=auth_header(admin_token),
    )
    assert detail_resp.status_code == 404


@pytest.mark.asyncio
async def test_reject_doctor_no_reason(db_app_client: AsyncClient, test_engine):
    """Rejection without a reason still works (reason is optional)."""
    email, password = await create_admin_user(test_engine, "rjd2")
    admin_token = await login(db_app_client, email, password)

    doctor_id = await register_doctor(db_app_client, "rjd2")

    with patch("src.core.email.send_email", return_value=True):
        resp = await db_app_client.post(
            f"/api/v1/admin/doctors/{doctor_id}/reject",
            headers=auth_header(admin_token),
            json={},
        )

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_reject_non_doctor_returns_400(db_app_client: AsyncClient, test_engine):
    """Rejecting a patient returns 400."""
    email, password = await create_admin_user(test_engine, "rjd3")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "rjd3")

    resp = await db_app_client.post(
        f"/api/v1/admin/doctors/{patient_id}/reject",
        headers=auth_header(admin_token),
        json={},
    )
    assert resp.status_code == 400


# ================================================================== #
# GET /api/v1/admin/cases/{case_id}
# ================================================================== #

@pytest.mark.asyncio
async def test_get_case_detail_returns_full_info(db_app_client: AsyncClient, test_engine):
    """Admin can fetch full case detail including patient name and AI status."""
    email, password = await create_admin_user(test_engine, "gcd1")
    admin_token = await login(db_app_client, email, password)

    patient_token, _ = await register_and_login_patient(db_app_client, "gcd1")
    await db_app_client.patch(
        "/api/v1/users/me",
        headers=auth_header(patient_token),
        json={"date_of_birth": "1990-01-01", "gender": "female"},
    )
    case_resp = await db_app_client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "is_for_self": True,
            "presenting_complaint": "Itchy rash on arm",
            "consent_ai_analysis": True,
        },
    )
    assert case_resp.status_code == 201
    case_id = case_resp.json()["id"]

    resp = await db_app_client.get(
        f"/api/v1/admin/cases/{case_id}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == case_id
    assert body["patient_name"] == "Admin Patient gcd1"
    assert body["ai_status"] == "pending"
    assert body["consultation_type"] == "new_complaint"
    assert body["presenting_complaint"] == "Itchy rash on arm"
    assert body["consent_ai_analysis"] is True
    assert "red_flag_status" in body


@pytest.mark.asyncio
async def test_get_case_detail_not_found(db_app_client: AsyncClient, test_engine):
    email, password = await create_admin_user(test_engine, "gcd2")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.get(
        "/api/v1/admin/cases/nonexistent-case-id",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


# ================================================================== #
# DELETE /api/v1/admin/users/{user_id}
# ================================================================== #

@pytest.mark.asyncio
async def test_delete_user_removes_account(db_app_client: AsyncClient, test_engine):
    """Admin can permanently delete a user account."""
    email, password = await create_admin_user(test_engine, "du1")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "du1")

    resp = await db_app_client.delete(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 204

    # Confirm gone
    detail_resp = await db_app_client.get(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(admin_token),
    )
    assert detail_resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_user_not_found(db_app_client: AsyncClient, test_engine):
    email, password = await create_admin_user(test_engine, "du2")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.delete(
        "/api/v1/admin/users/no-such-user",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_user_requires_admin(db_app_client: AsyncClient, test_engine):
    patient_token, patient_id = await register_and_login_patient(db_app_client, "du3")

    resp = await db_app_client.delete(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 403


# ================================================================== #
# POST /api/v1/admin/users/{user_id}/resend-verification
# ================================================================== #

@pytest.mark.asyncio
async def test_resend_verification_for_unverified_user(db_app_client: AsyncClient, test_engine):
    """Admin can resend verification OTP to an unverified user."""
    email, password = await create_admin_user(test_engine, "rv1")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "rv1")
    # Newly registered patients are not yet verified

    with patch("src.core.email.send_email", return_value=True):
        resp = await db_app_client.post(
            f"/api/v1/admin/users/{patient_id}/resend-verification",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_resend_verification_already_verified_returns_400(
    db_app_client: AsyncClient, test_engine
):
    """Resending to an already verified user returns 400."""
    email, password = await create_admin_user(test_engine, "rv2")
    admin_token = await login(db_app_client, email, password)

    _, patient_id = await register_and_login_patient(db_app_client, "rv2")

    # Verify the user first via admin PATCH
    await db_app_client.patch(
        f"/api/v1/admin/users/{patient_id}",
        headers=auth_header(admin_token),
        json={"is_verified": True},
    )

    resp = await db_app_client.post(
        f"/api/v1/admin/users/{patient_id}/resend-verification",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_resend_verification_not_found(db_app_client: AsyncClient, test_engine):
    email, password = await create_admin_user(test_engine, "rv3")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.post(
        "/api/v1/admin/users/no-such-id/resend-verification",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


# ================================================================== #
# GET /api/v1/admin/prompts
# ================================================================== #

@pytest.mark.asyncio
async def test_get_prompts_returns_all_keys(db_app_client: AsyncClient, test_engine):
    """Admin can retrieve the full prompt list — always 10 keys."""
    email, password = await create_admin_user(test_engine, "gp1")
    admin_token = await login(db_app_client, email, password)

    with patch("src.ai.prompt_registry._redis_get", return_value=None):
        resp = await db_app_client.get(
            "/api/v1/admin/prompts",
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "prompts" in body
    assert len(body["prompts"]) == 10
    for item in body["prompts"]:
        assert "key" in item
        assert "label" in item
        assert "has_override" in item


@pytest.mark.asyncio
async def test_get_prompts_requires_admin(db_app_client: AsyncClient, test_engine):
    patient_token, _ = await register_and_login_patient(db_app_client, "gp2")
    resp = await db_app_client.get(
        "/api/v1/admin/prompts",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 403


# ================================================================== #
# PATCH /api/v1/admin/prompts/{key}
# ================================================================== #

@pytest.mark.asyncio
async def test_update_prompt_sets_override(db_app_client: AsyncClient, test_engine):
    """Setting a prompt override stores the value and has_override becomes True."""
    email, password = await create_admin_user(test_engine, "up1")
    admin_token = await login(db_app_client, email, password)

    custom_prompt = "You are a custom dermatology assistant. {follow_up_context}"

    with patch("src.ai.prompt_registry._redis_set") as mock_set, \
         patch("src.ai.prompt_registry._redis_get", return_value=custom_prompt):
        resp = await db_app_client.patch(
            "/api/v1/admin/prompts/patient_first_question",
            headers=auth_header(admin_token),
            json={"value": custom_prompt},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "patient_first_question"
    assert body["has_override"] is True
    assert body["value"] == custom_prompt
    mock_set.assert_called_once()


@pytest.mark.asyncio
async def test_update_prompt_invalid_key_returns_400(db_app_client: AsyncClient, test_engine):
    """Updating a non-existent prompt key returns 400."""
    email, password = await create_admin_user(test_engine, "up2")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.patch(
        "/api/v1/admin/prompts/nonexistent_key",
        headers=auth_header(admin_token),
        json={"value": "some value"},
    )
    assert resp.status_code == 400


# ================================================================== #
# DELETE /api/v1/admin/prompts/{key}
# ================================================================== #

@pytest.mark.asyncio
async def test_reset_prompt_removes_override(db_app_client: AsyncClient, test_engine):
    """Resetting a prompt removes the Redis override (has_override becomes False)."""
    email, password = await create_admin_user(test_engine, "rp1")
    admin_token = await login(db_app_client, email, password)

    with patch("src.ai.prompt_registry._redis_delete") as mock_del, \
         patch("src.ai.prompt_registry._redis_get", return_value=None):
        resp = await db_app_client.delete(
            "/api/v1/admin/prompts/doctor_diagnosis",
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "doctor_diagnosis"
    assert body["has_override"] is False
    assert body["value"] is None
    mock_del.assert_called_once()


@pytest.mark.asyncio
async def test_reset_prompt_invalid_key_returns_400(db_app_client: AsyncClient, test_engine):
    """Resetting a non-existent prompt key returns 400."""
    email, password = await create_admin_user(test_engine, "rp2")
    admin_token = await login(db_app_client, email, password)

    resp = await db_app_client.delete(
        "/api/v1/admin/prompts/bad_key",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400
