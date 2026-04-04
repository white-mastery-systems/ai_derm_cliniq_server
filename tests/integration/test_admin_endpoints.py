"""
tests/integration/test_admin_endpoints.py — Admin Endpoint Integration Tests
=============================================================================

Tests for:
    GET   /api/v1/admin/users              — list users (filtered)
    GET   /api/v1/admin/users/{user_id}    — get user detail
    PATCH /api/v1/admin/users/{user_id}    — update account state
    GET   /api/v1/admin/cases              — list all cases
    GET   /api/v1/admin/stats              — platform statistics

ADMIN USER SETUP
----------------
There is no POST /auth/register/admin endpoint — admins are created
via direct DB insertion. Each test module has a `create_admin` helper
that inserts a User row (role=admin) directly using test_engine,
then logs in via POST /auth/login to obtain a JWT.
"""

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


async def register_and_login_doctor(client: AsyncClient, suffix: str) -> tuple[str, str]:
    await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Admin Doctor {suffix}",
        "email": f"adm_doctor_{suffix}@admtest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-ADM-{suffix}",
        "clinic_name": "Admin Clinic",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"adm_doctor_{suffix}@admtest.com",
        "password": "DocPass9",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


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
    await db_app_client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={"consultation_type": "new_complaint", "has_visible_lesion": True,
              "is_for_self": True, "presenting_complaint": "Test"},
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
