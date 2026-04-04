"""
tests/integration/test_auth_endpoints.py — Auth Endpoint Integration Tests
===========================================================================

These tests exercise the full HTTP stack:
    HTTP request → FastAPI router → service → DB → HTTP response

WHAT WE TEST
------------
1. POST /api/v1/auth/register/patient
2. POST /api/v1/auth/register/doctor
3. POST /api/v1/auth/login
4. POST /api/v1/auth/refresh
5. POST /api/v1/auth/logout
6. Error cases: duplicate email, wrong password, bad tokens

WHY INTEGRATION (NOT UNIT)?
-----------------------------
Auth logic involves the DB (creating users, storing token hashes, revoking tokens).
Unit tests can't verify this. Integration tests run the full stack against a real
SQLite database (our test_engine) to confirm the complete flow works end-to-end.

FIXTURE USED: db_app_client
----------------------------
This fixture overrides get_async_session to redirect all DB calls to the shared
test engine (where tables were created by the session fixture in conftest.py).
Each test gets a response, and the DB state accumulates within the session.
We use unique emails per test to avoid cross-test collisions.

NOTE ON ISOLATION
-----------------
The `db_session` fixture rolls back after each test, but `db_app_client` sessions
go through the route's session dependency (committed, not rolled back). We use
unique emails per test class to prevent email-uniqueness conflicts.
"""

import pytest_asyncio
from httpx import AsyncClient


# ================================================================== #
# Helpers
# ================================================================== #

def patient_payload(suffix: str = "") -> dict:
    return {
        "full_name": f"Test Patient{suffix}",
        "email": f"patient{suffix}@test.com",
        "password": "TestPass1",
    }


def doctor_payload(suffix: str = "") -> dict:
    return {
        "full_name": f"Test Doctor{suffix}",
        "email": f"doctor{suffix}@test.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": "LIC-001",
        "clinic_name": "Skin Clinic",
    }


# ================================================================== #
# Patient Registration
# ================================================================== #

class TestPatientRegistration:
    async def test_register_patient_returns_201(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/register/patient",
            json=patient_payload("_reg1"),
        )
        assert response.status_code == 201

    async def test_register_patient_response_has_tokens(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/register/patient",
            json=patient_payload("_reg2"),
        )
        body = response.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"

    async def test_register_patient_response_has_user(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/register/patient",
            json=patient_payload("_reg3"),
        )
        body = response.json()
        user = body["user"]
        assert user["email"] == "patient_reg3@test.com"
        assert user["role"] == "patient"
        assert user["is_active"] is True

    async def test_register_patient_has_patient_code(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/register/patient",
            json=patient_payload("_reg4"),
        )
        body = response.json()
        patient_code = body["user"]["patient_code"]
        assert patient_code is not None
        # Format: AAA-9999-Z
        import re
        assert re.match(r"^[A-Z]{3}-\d{4}-[A-Z]$", patient_code)

    async def test_register_patient_duplicate_email_returns_409(
        self, db_app_client: AsyncClient
    ):
        payload = patient_payload("_dup")
        await db_app_client.post("/api/v1/auth/register/patient", json=payload)
        # Second registration with same email
        response = await db_app_client.post(
            "/api/v1/auth/register/patient", json=payload
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error"]["code"] == "EMAIL_ALREADY_REGISTERED"

    async def test_register_patient_weak_password_returns_422(
        self, db_app_client: AsyncClient
    ):
        payload = {**patient_payload("_weak"), "password": "allletter"}  # no digit
        response = await db_app_client.post(
            "/api/v1/auth/register/patient", json=payload
        )
        assert response.status_code == 422

    async def test_register_patient_short_password_returns_422(
        self, db_app_client: AsyncClient
    ):
        payload = {**patient_payload("_short"), "password": "Ab1"}
        response = await db_app_client.post(
            "/api/v1/auth/register/patient", json=payload
        )
        assert response.status_code == 422

    async def test_register_patient_invalid_email_returns_422(
        self, db_app_client: AsyncClient
    ):
        payload = {**patient_payload("_inv"), "email": "not-an-email"}
        response = await db_app_client.post(
            "/api/v1/auth/register/patient", json=payload
        )
        assert response.status_code == 422

    async def test_register_patient_missing_fields_returns_422(
        self, db_app_client: AsyncClient
    ):
        response = await db_app_client.post(
            "/api/v1/auth/register/patient",
            json={"email": "x@x.com"},  # missing full_name and password
        )
        assert response.status_code == 422


# ================================================================== #
# Doctor Registration
# ================================================================== #

class TestDoctorRegistration:
    async def test_register_doctor_returns_201(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/register/doctor",
            json=doctor_payload("_reg1"),
        )
        assert response.status_code == 201

    async def test_register_doctor_role_is_doctor(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/register/doctor",
            json=doctor_payload("_reg2"),
        )
        assert response.json()["user"]["role"] == "doctor"

    async def test_register_doctor_no_patient_code(self, db_app_client: AsyncClient):
        """Doctors don't get a patient_code."""
        response = await db_app_client.post(
            "/api/v1/auth/register/doctor",
            json=doctor_payload("_reg3"),
        )
        assert response.json()["user"]["patient_code"] is None

    async def test_register_doctor_optional_fields_can_be_null(
        self, db_app_client: AsyncClient
    ):
        payload = {
            "full_name": "Dr Minimal",
            "email": "drminimal@test.com",
            "password": "DocPass9",
        }
        response = await db_app_client.post(
            "/api/v1/auth/register/doctor", json=payload
        )
        assert response.status_code == 201

    async def test_register_doctor_duplicate_email_returns_409(
        self, db_app_client: AsyncClient
    ):
        payload = doctor_payload("_dup")
        await db_app_client.post("/api/v1/auth/register/doctor", json=payload)
        response = await db_app_client.post(
            "/api/v1/auth/register/doctor", json=payload
        )
        assert response.status_code == 409


# ================================================================== #
# Login
# ================================================================== #

class TestLogin:
    @pytest_asyncio.fixture(autouse=True)
    async def _register_patient(self, db_app_client: AsyncClient):
        """Register a patient before each test in this class."""
        await db_app_client.post(
            "/api/v1/auth/register/patient",
            json=patient_payload("_login"),
        )

    async def test_login_returns_200(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/login",
            json={"email": "patient_login@test.com", "password": "TestPass1"},
        )
        assert response.status_code == 200

    async def test_login_response_has_tokens(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/login",
            json={"email": "patient_login@test.com", "password": "TestPass1"},
        )
        body = response.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["role"] == "patient"

    async def test_login_wrong_password_returns_401(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/login",
            json={"email": "patient_login@test.com", "password": "WrongPass1"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_login_wrong_email_returns_401(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@test.com", "password": "TestPass1"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_login_missing_password_returns_422(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/login",
            json={"email": "patient_login@test.com"},
        )
        assert response.status_code == 422


# ================================================================== #
# Token Refresh
# ================================================================== #

class TestTokenRefresh:
    @pytest_asyncio.fixture
    async def refresh_token(self, db_app_client: AsyncClient) -> str:
        """Register + login to get a refresh token."""
        await db_app_client.post(
            "/api/v1/auth/register/patient",
            json=patient_payload("_refresh"),
        )
        login_resp = await db_app_client.post(
            "/api/v1/auth/login",
            json={"email": "patient_refresh@test.com", "password": "TestPass1"},
        )
        return login_resp.json()["refresh_token"]

    async def test_refresh_returns_200(
        self, db_app_client: AsyncClient, refresh_token: str
    ):
        response = await db_app_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert response.status_code == 200

    async def test_refresh_returns_new_tokens(
        self, db_app_client: AsyncClient, refresh_token: str
    ):
        response = await db_app_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        body = response.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["refresh_token"] != refresh_token  # rotated

    async def test_refresh_old_token_is_revoked(
        self, db_app_client: AsyncClient, refresh_token: str
    ):
        """Using a token twice should fail (token rotation)."""
        await db_app_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        # Use the old token again — should be rejected
        response = await db_app_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert response.status_code == 401

    async def test_refresh_with_garbage_token_returns_401(
        self, db_app_client: AsyncClient
    ):
        response = await db_app_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "garbage.token.value"},
        )
        assert response.status_code == 401

    async def test_refresh_with_access_token_returns_401(
        self, db_app_client: AsyncClient
    ):
        """Must not accept an access token in the refresh endpoint."""
        # First get a valid access token from the login
        login_resp = await db_app_client.post(
            "/api/v1/auth/login",
            json={"email": "patient_refresh@test.com", "password": "TestPass1"},
        )
        access_token = login_resp.json()["access_token"]
        response = await db_app_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": access_token},
        )
        assert response.status_code == 401


# ================================================================== #
# Logout
# ================================================================== #

class TestLogout:
    @pytest_asyncio.fixture
    async def refresh_token(self, db_app_client: AsyncClient) -> str:
        await db_app_client.post(
            "/api/v1/auth/register/patient",
            json=patient_payload("_logout"),
        )
        login_resp = await db_app_client.post(
            "/api/v1/auth/login",
            json={"email": "patient_logout@test.com", "password": "TestPass1"},
        )
        return login_resp.json()["refresh_token"]

    async def test_logout_returns_204(
        self, db_app_client: AsyncClient, refresh_token: str
    ):
        response = await db_app_client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refresh_token},
        )
        assert response.status_code == 204

    async def test_logout_no_body(
        self, db_app_client: AsyncClient, refresh_token: str
    ):
        response = await db_app_client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refresh_token},
        )
        assert response.content == b""

    async def test_logout_revokes_refresh_token(
        self, db_app_client: AsyncClient, refresh_token: str
    ):
        """After logout, the refresh token must no longer work."""
        await db_app_client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refresh_token},
        )
        response = await db_app_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert response.status_code == 401

    async def test_logout_with_unknown_token_returns_204(
        self, db_app_client: AsyncClient
    ):
        """Logout is idempotent — unknown tokens silently succeed."""
        from src.auth.security import generate_refresh_token

        # A valid random token that has never been stored in the DB
        fake_token = generate_refresh_token()
        response = await db_app_client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": fake_token},
        )
        assert response.status_code == 204


# ================================================================== #
# Auth Error Responses
# ================================================================== #

class TestAuthErrorFormat:
    async def test_validation_error_has_error_key(self, db_app_client: AsyncClient):
        response = await db_app_client.post(
            "/api/v1/auth/register/patient",
            json={},
        )
        assert response.status_code == 422
        body = response.json()
        assert "error" in body
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "fields" in body["error"]["details"]

    async def test_conflict_error_has_error_key(self, db_app_client: AsyncClient):
        payload = patient_payload("_err")
        await db_app_client.post("/api/v1/auth/register/patient", json=payload)
        response = await db_app_client.post(
            "/api/v1/auth/register/patient", json=payload
        )
        body = response.json()
        assert "error" in body
        assert body["error"]["status"] == 409

    async def test_invalid_credentials_error_has_error_key(
        self, db_app_client: AsyncClient
    ):
        response = await db_app_client.post(
            "/api/v1/auth/login",
            json={"email": "noone@test.com", "password": "Pass1234"},
        )
        body = response.json()
        assert "error" in body
        assert body["error"]["status"] == 401
