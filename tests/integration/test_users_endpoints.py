"""
tests/integration/test_users_endpoints.py — Users Endpoint Integration Tests
=============================================================================

Tests for the /api/v1/users endpoints:
    GET    /api/v1/users/me       — fetch own profile
    PATCH  /api/v1/users/me       — update profile
    DELETE /api/v1/users/me       — soft-delete account

FIXTURE: db_app_client
-----------------------
Routes all DB calls to the shared test engine (SQLite in-memory).
We register + login in each test class using unique emails to avoid
cross-test conflicts (the test session shares one DB).

AUTH FLOW
---------
Every protected route requires `Authorization: Bearer <token>`.
We register a user via POST /auth/register/*, login to get the access token,
then pass that token in the Authorization header for subsequent calls.
"""

import pytest
from httpx import AsyncClient


# ================================================================== #
# Helpers
# ================================================================== #

async def register_and_login_patient(client: AsyncClient, suffix: str) -> str:
    """Register a patient and return the access token."""
    await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"Test Patient {suffix}",
        "email": f"patient_{suffix}@userstest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"patient_{suffix}@userstest.com",
        "password": "TestPass1",
    })
    return resp.json()["access_token"]


async def register_and_login_doctor(client: AsyncClient, suffix: str) -> str:
    """Register a doctor and return the access token."""
    await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Test Doctor {suffix}",
        "email": f"doctor_{suffix}@userstest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-{suffix}",
        "clinic_name": "Skin Clinic",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"doctor_{suffix}@userstest.com",
        "password": "DocPass9",
    })
    return resp.json()["access_token"]


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ================================================================== #
# GET /api/v1/users/me
# ================================================================== #

class TestGetMyProfile:

    async def test_patient_gets_own_profile(self, db_app_client: AsyncClient):
        token = await register_and_login_patient(db_app_client, "get01")
        resp = await db_app_client.get("/api/v1/users/me", headers=auth_header(token))

        assert resp.status_code == 200
        body = resp.json()
        assert body["email"] == "patient_get01@userstest.com"
        assert body["role"] == "patient"
        assert body["is_active"] is True
        assert body["patient_profile"] is not None
        assert "patient_code" in body["patient_profile"]
        assert body["doctor_profile"] is None

    async def test_doctor_gets_own_profile(self, db_app_client: AsyncClient):
        token = await register_and_login_doctor(db_app_client, "get02")
        resp = await db_app_client.get("/api/v1/users/me", headers=auth_header(token))

        assert resp.status_code == 200
        body = resp.json()
        assert body["email"] == "doctor_get02@userstest.com"
        assert body["role"] == "doctor"
        assert body["doctor_profile"] is not None
        assert body["doctor_profile"]["specialization"] == "Dermatology"
        assert body["patient_profile"] is None

    async def test_missing_token_returns_401(self, db_app_client: AsyncClient):
        resp = await db_app_client.get("/api/v1/users/me")
        assert resp.status_code == 401

    async def test_invalid_token_returns_401(self, db_app_client: AsyncClient):
        resp = await db_app_client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401


# ================================================================== #
# PATCH /api/v1/users/me
# ================================================================== #

class TestUpdateMyProfile:

    async def test_patient_can_update_full_name(self, db_app_client: AsyncClient):
        token = await register_and_login_patient(db_app_client, "upd01")
        resp = await db_app_client.patch(
            "/api/v1/users/me",
            headers=auth_header(token),
            json={"full_name": "Updated Name"},
        )
        assert resp.status_code == 200
        assert resp.json()["full_name"] == "Updated Name"

    async def test_patient_can_update_dob_and_gender(self, db_app_client: AsyncClient):
        token = await register_and_login_patient(db_app_client, "upd02")
        resp = await db_app_client.patch(
            "/api/v1/users/me",
            headers=auth_header(token),
            json={"date_of_birth": "1990-06-15", "gender": "Female"},
        )
        assert resp.status_code == 200
        profile = resp.json()["patient_profile"]
        assert profile["date_of_birth"] == "1990-06-15"
        assert profile["gender"] == "Female"

    async def test_patient_sending_doctor_fields_is_ignored(self, db_app_client: AsyncClient):
        token = await register_and_login_patient(db_app_client, "upd03")
        resp = await db_app_client.patch(
            "/api/v1/users/me",
            headers=auth_header(token),
            json={"specialization": "Cardiology"},
        )
        # Should not error — doctor-only fields silently ignored for patients
        assert resp.status_code == 200
        assert resp.json()["patient_profile"] is not None

    async def test_doctor_can_update_clinic_name(self, db_app_client: AsyncClient):
        token = await register_and_login_doctor(db_app_client, "upd04")
        resp = await db_app_client.patch(
            "/api/v1/users/me",
            headers=auth_header(token),
            json={"clinic_name": "New Clinic"},
        )
        assert resp.status_code == 200
        assert resp.json()["doctor_profile"]["clinic_name"] == "New Clinic"

    async def test_doctor_can_toggle_notifications(self, db_app_client: AsyncClient):
        token = await register_and_login_doctor(db_app_client, "upd05")
        resp = await db_app_client.patch(
            "/api/v1/users/me",
            headers=auth_header(token),
            json={"notifications_enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["doctor_profile"]["notifications_enabled"] is False

    async def test_update_without_token_returns_401(self, db_app_client: AsyncClient):
        resp = await db_app_client.patch(
            "/api/v1/users/me",
            json={"full_name": "Hacker"},
        )
        assert resp.status_code == 401

    async def test_empty_patch_is_accepted(self, db_app_client: AsyncClient):
        """An empty PATCH body is valid — it's a no-op update."""
        token = await register_and_login_patient(db_app_client, "upd06")
        resp = await db_app_client.patch(
            "/api/v1/users/me",
            headers=auth_header(token),
            json={},
        )
        assert resp.status_code == 200


# ================================================================== #
# DELETE /api/v1/users/me
# ================================================================== #

class TestDeleteMyProfile:

    async def test_patient_can_soft_delete_account(self, db_app_client: AsyncClient):
        token = await register_and_login_patient(db_app_client, "del01")
        resp = await db_app_client.delete(
            "/api/v1/users/me",
            headers=auth_header(token),
        )
        assert resp.status_code == 204

    async def test_after_soft_delete_profile_is_inaccessible(self, db_app_client: AsyncClient):
        """
        After soft-deletion, the access token is still technically valid (JWT),
        but the DB lookup in get_current_user finds is_active=False → 401.
        """
        token = await register_and_login_patient(db_app_client, "del02")
        await db_app_client.delete("/api/v1/users/me", headers=auth_header(token))
        resp = await db_app_client.get("/api/v1/users/me", headers=auth_header(token))
        assert resp.status_code == 401

    async def test_delete_without_token_returns_401(self, db_app_client: AsyncClient):
        resp = await db_app_client.delete("/api/v1/users/me")
        assert resp.status_code == 401
