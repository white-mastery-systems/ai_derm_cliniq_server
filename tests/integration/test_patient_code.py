"""
tests/integration/test_patient_code.py — Patient Code Backward Compatibility
=============================================================================

Tests for the patient code system:
    - Code is returned on registration (backward compat with older Flutter clients)
    - Code format is preserved in the DB (AAA-9999-Z)
    - GET /api/v1/users/by-code/{code} — doctor-only lookup
    - Case-insensitive lookup (service calls .upper() internally)
    - 404 for unknown codes
    - 401 for unauthenticated access
    - 403 for patients (doctor-only endpoint)
    - Code is visible in GET /users/me patient_profile

WHY "BACKWARD COMPATIBILITY"?
------------------------------
The patient_code field was introduced early and the format
(AAA-9999-Z) must remain stable — older app versions may display it
as a QR / shareable string. We must never change the format, length,
or character set without a migration plan.
"""

import re

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from unittest.mock import patch

from src.auth.security import hash_password
from src.models.base import new_uuid
from src.models.user import User, UserRole


PATIENT_CODE_PATTERN = re.compile(r"^[A-Z]{3}-\d{4}-[A-Z]$")


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def register_patient(client: AsyncClient, suffix: str) -> dict:
    """Register a patient and return the registration response body.

    patient_code lives at body["user"]["patient_code"] — the top-level body
    also has access_token / refresh_token. Callers use body["user"]["patient_code"].
    """
    resp = await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"Code Patient {suffix}",
        "email": f"code_patient_{suffix}@codetest.com",
        "password": "TestPass1",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _create_admin_and_approve_doctor(client: AsyncClient, test_engine, doctor_id: str) -> None:
    admin_email = f"_tmp_admin_{doctor_id[:8]}@codetest.com"
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        admin = User(
            id=new_uuid(),
            email=admin_email,
            full_name="Temp Admin",
            role=UserRole.ADMIN,
            password_hash=hash_password("AdminPass9"),
            is_active=True,
            is_verified=True,
        )
        session.add(admin)
        await session.commit()
    login_resp = await client.post("/api/v1/auth/login", json={"email": admin_email, "password": "AdminPass9"})
    admin_token = login_resp.json()["access_token"]
    with patch("src.core.email.send_email", return_value=True):
        await client.post(
            f"/api/v1/admin/doctors/{doctor_id}/approve",
            headers=auth_header(admin_token),
        )


async def register_and_login_doctor(client: AsyncClient, suffix: str, test_engine) -> str:
    reg_resp = await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Code Doctor {suffix}",
        "email": f"code_doctor_{suffix}@codetest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-CODE-{suffix}",
        "clinic_name": "Code Clinic",
    })
    doctor_id = reg_resp.json()["user"]["id"]
    await _create_admin_and_approve_doctor(client, test_engine, doctor_id)
    return await login(client, f"code_doctor_{suffix}@codetest.com", "DocPass9")


# ================================================================== #
# Registration returns patient_code
# ================================================================== #

class TestRegistrationReturnsCode:

    async def test_patient_registration_includes_patient_code(self, db_app_client: AsyncClient):
        """The /auth/register/patient response must include patient_code (backward compat).

        patient_code is nested under body["user"] — the top-level also has
        access_token / refresh_token.
        """
        body = await register_patient(db_app_client, "reg01")
        assert "patient_code" in body["user"], "patient_code missing from registration response"
        assert body["user"]["patient_code"] is not None

    async def test_patient_code_format_on_registration(self, db_app_client: AsyncClient):
        """Registered code must match AAA-9999-Z format."""
        body = await register_patient(db_app_client, "reg02")
        code = body["user"]["patient_code"]
        assert PATIENT_CODE_PATTERN.match(code), f"Bad format: {code!r}"

    async def test_patient_code_length_is_ten_chars(self, db_app_client: AsyncClient):
        """AAA-9999-Z = exactly 10 characters."""
        body = await register_patient(db_app_client, "reg03")
        assert len(body["user"]["patient_code"]) == 10

    async def test_two_patients_get_different_codes(self, db_app_client: AsyncClient):
        """Each patient must receive a unique code."""
        body1 = await register_patient(db_app_client, "reg04a")
        body2 = await register_patient(db_app_client, "reg04b")
        assert body1["user"]["patient_code"] != body2["user"]["patient_code"]


# ================================================================== #
# Code visible in profile
# ================================================================== #

class TestCodeInProfile:

    async def test_patient_code_visible_in_get_me(self, db_app_client: AsyncClient):
        """GET /users/me must expose patient_code inside patient_profile."""
        body = await register_patient(db_app_client, "prof01")
        token = await login(db_app_client, "code_patient_prof01@codetest.com", "TestPass1")

        profile_resp = await db_app_client.get("/api/v1/users/me", headers=auth_header(token))
        assert profile_resp.status_code == 200
        profile = profile_resp.json()
        assert profile["patient_profile"] is not None
        code = profile["patient_profile"]["patient_code"]
        assert PATIENT_CODE_PATTERN.match(code), f"Bad format: {code!r}"

    async def test_profile_code_matches_registration_code(self, db_app_client: AsyncClient):
        """The code returned at registration must be the same as in the profile."""
        body = await register_patient(db_app_client, "prof02")
        reg_code = body["user"]["patient_code"]
        token = await login(db_app_client, "code_patient_prof02@codetest.com", "TestPass1")

        profile_resp = await db_app_client.get("/api/v1/users/me", headers=auth_header(token))
        profile_code = profile_resp.json()["patient_profile"]["patient_code"]
        assert reg_code == profile_code


# ================================================================== #
# GET /api/v1/users/by-code/{code} — doctor lookup
# ================================================================== #

class TestGetPatientByCode:

    async def test_doctor_can_look_up_patient_by_exact_code(
        self, db_app_client: AsyncClient, test_engine
    ):
        """A valid patient code returns patient info to an approved doctor."""
        reg_body = await register_patient(db_app_client, "lkp01")
        patient_code = reg_body["user"]["patient_code"]

        d_token = await register_and_login_doctor(db_app_client, "lkp01", test_engine)

        resp = await db_app_client.get(
            f"/api/v1/users/by-code/{patient_code}",
            headers=auth_header(d_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["patient_code"] == patient_code
        assert "full_name" in body
        assert "user_id" in body  # PatientByCodeResponse uses user_id, not id

    async def test_lookup_is_case_insensitive(
        self, db_app_client: AsyncClient, test_engine
    ):
        """
        Doctors may type lowercase — the service calls .upper() internally.
        Lowercase input must resolve to the same patient as the stored uppercase code.
        """
        reg_body = await register_patient(db_app_client, "lkp02")
        patient_code = reg_body["user"]["patient_code"]   # e.g. "ABC-1234-Z"
        lowercase_code = patient_code.lower()             # e.g. "abc-1234-z"

        d_token = await register_and_login_doctor(db_app_client, "lkp02", test_engine)

        resp = await db_app_client.get(
            f"/api/v1/users/by-code/{lowercase_code}",
            headers=auth_header(d_token),
        )
        assert resp.status_code == 200
        assert resp.json()["patient_code"] == patient_code

    async def test_unknown_code_returns_404(
        self, db_app_client: AsyncClient, test_engine
    ):
        """A code that doesn't exist in the DB returns 404, not 500."""
        d_token = await register_and_login_doctor(db_app_client, "lkp03", test_engine)
        resp = await db_app_client.get(
            "/api/v1/users/by-code/ZZZ-0000-Z",
            headers=auth_header(d_token),
        )
        assert resp.status_code == 404

    async def test_malformed_code_returns_404(
        self, db_app_client: AsyncClient, test_engine
    ):
        """A code with wrong format is simply not found (no validation error — it just won't match)."""
        d_token = await register_and_login_doctor(db_app_client, "lkp04", test_engine)
        resp = await db_app_client.get(
            "/api/v1/users/by-code/NOT-A-REAL-CODE",
            headers=auth_header(d_token),
        )
        assert resp.status_code == 404

    async def test_patient_cannot_use_by_code_endpoint(self, db_app_client: AsyncClient):
        """GET /users/by-code/ is doctor-only — patients must get 403."""
        reg_body = await register_patient(db_app_client, "lkp05a")
        patient_code = reg_body["user"]["patient_code"]
        p_token = await login(db_app_client, "code_patient_lkp05a@codetest.com", "TestPass1")

        resp = await db_app_client.get(
            f"/api/v1/users/by-code/{patient_code}",
            headers=auth_header(p_token),
        )
        assert resp.status_code == 403

    async def test_unauthenticated_cannot_use_by_code_endpoint(self, db_app_client: AsyncClient):
        """No token → 401."""
        resp = await db_app_client.get("/api/v1/users/by-code/ABC-1234-Z")
        assert resp.status_code == 401

    async def test_response_does_not_leak_sensitive_fields(
        self, db_app_client: AsyncClient, test_engine
    ):
        """
        The by-code response (PatientByCodeResponse) must not include
        password_hash, fcm_token, or other internal fields.
        """
        reg_body = await register_patient(db_app_client, "lkp06")
        patient_code = reg_body["user"]["patient_code"]
        d_token = await register_and_login_doctor(db_app_client, "lkp06", test_engine)

        resp = await db_app_client.get(
            f"/api/v1/users/by-code/{patient_code}",
            headers=auth_header(d_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "password_hash" not in body
        assert "fcm_token" not in body
        assert "refresh_tokens" not in body
