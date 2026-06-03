"""
tests/integration/test_cases_endpoints.py — Cases Endpoint Integration Tests
=============================================================================

Tests for the /api/v1/cases endpoints:
    POST   /api/v1/cases                    — create a case
    GET    /api/v1/cases                    — list cases (role-filtered)
    GET    /api/v1/cases/{id}               — get full case detail
    PATCH  /api/v1/cases/{id}               — update (complaint, clinical status)
    DELETE /api/v1/cases/{id}               — soft cancel
    PATCH  /api/v1/cases/{id}/assign        — doctor claims case
    GET    /api/v1/cases/doctors/me/stats   — doctor dashboard stats

KEY BEHAVIORS UNDER TEST
-------------------------
- Patients only see their own cases
- Doctors only see assigned cases
- CaseNotFoundException (404) returned for access-denied (not 403) — enumeration safe
- consent_ai_analysis is required at case creation (and is immutable)
- presenting_complaint is patient-only, clinical_status is doctor-only
- Soft-delete sets ai_status=failed, clinical_status=resolved
- Doctor assign is idempotent; different doctor → 403
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

async def register_and_login_patient(client: AsyncClient, suffix: str) -> tuple[str, str]:
    """Register patient with complete profile (date_of_birth/gender required for case creation)."""
    await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"Case Patient {suffix}",
        "email": f"case_patient_{suffix}@casestest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"case_patient_{suffix}@casestest.com",
        "password": "TestPass1",
    })
    token = resp.json()["access_token"]
    # Profile must be complete before case creation (date_of_birth + gender required)
    await client.patch(
        "/api/v1/users/me",
        headers=auth_header(token),
        json={"date_of_birth": "1990-01-01", "gender": "female"},
    )
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def _create_admin_and_approve_doctor(client: AsyncClient, test_engine, doctor_id: str) -> None:
    """Insert a temporary admin and use it to approve the pending doctor."""
    admin_email = f"_tmp_admin_{doctor_id[:8]}@casestest.com"
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

    login_resp = await client.post("/api/v1/auth/login", json={
        "email": admin_email,
        "password": "AdminPass9",
    })
    admin_token = login_resp.json()["access_token"]

    with patch("src.core.email.send_email", return_value=True):
        await client.post(
            f"/api/v1/admin/doctors/{doctor_id}/approve",
            headers=auth_header(admin_token),
        )


async def register_and_login_doctor(
    client: AsyncClient, suffix: str, test_engine=None
) -> tuple[str, str]:
    """Register a doctor, approve it (requires test_engine), then log in."""
    reg_resp = await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Case Doctor {suffix}",
        "email": f"case_doctor_{suffix}@casestest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-CASE-{suffix}",
        "clinic_name": "Derm Clinic",
    })
    doctor_id = reg_resp.json()["user"]["id"]

    if test_engine is not None:
        await _create_admin_and_approve_doctor(client, test_engine, doctor_id)

    resp = await client.post("/api/v1/auth/login", json={
        "email": f"case_doctor_{suffix}@casestest.com",
        "password": "DocPass9",
    })
    token = resp.json()["access_token"]
    return token, doctor_id


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def case_payload(**overrides) -> dict:
    base = {
        "consultation_type": "new_complaint",
        "has_visible_lesion": True,
        "is_for_self": True,
        "presenting_complaint": "Red itchy rash on arm",
        "consent_ai_analysis": True,
    }
    base.update(overrides)
    return base


# ================================================================== #
# POST /api/v1/cases
# ================================================================== #

class TestCreateCase:

    async def test_patient_can_create_case(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "cr01")
        resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(token),
            json=case_payload(),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["ai_status"] == "pending"
        assert body["clinical_status"] == "active"
        assert body["consent_ai_analysis"] is True
        assert body["image_count"] == 0

    async def test_case_for_dependent_requires_dependent_info(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "cr02")
        resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(token),
            json=case_payload(is_for_self=False),  # no dependent field
        )
        assert resp.status_code == 400

    async def test_case_for_dependent_with_info_succeeds(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "cr03")
        resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(token),
            json=case_payload(
                is_for_self=False,
                dependent={
                    "name": "Child Name",
                    "relationship": "Child",
                    "date_of_birth": "2015-03-10",
                    "gender": "Male",
                },
            ),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["dependent_name"] == "Child Name"
        assert body["is_for_self"] is False

    async def test_doctor_cannot_create_case(self, db_app_client: AsyncClient, test_engine):
        token, _ = await register_and_login_doctor(db_app_client, "cr04", test_engine)
        resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(token),
            json=case_payload(),
        )
        assert resp.status_code == 403

    async def test_unauthenticated_cannot_create_case(self, db_app_client: AsyncClient):
        resp = await db_app_client.post("/api/v1/cases", json=case_payload())
        assert resp.status_code == 401


# ================================================================== #
# GET /api/v1/cases
# ================================================================== #

class TestListCases:

    async def test_patient_sees_only_own_cases(self, db_app_client: AsyncClient):
        token1, _ = await register_and_login_patient(db_app_client, "ls01")
        token2, _ = await register_and_login_patient(db_app_client, "ls02")

        # patient1 creates 2 cases
        for _ in range(2):
            await db_app_client.post("/api/v1/cases", headers=auth_header(token1), json=case_payload())

        # patient2 creates 1 case
        await db_app_client.post("/api/v1/cases", headers=auth_header(token2), json=case_payload())

        resp = await db_app_client.get("/api/v1/cases", headers=auth_header(token1))
        assert resp.status_code == 200
        body = resp.json()
        # patient1 should only see their 2 cases (not patient2's)
        assert body["total"] == 2
        assert len(body["items"]) == 2

    async def test_doctor_sees_only_assigned_cases(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "ls03")
        d_token, _ = await register_and_login_doctor(db_app_client, "ls03", test_engine)

        # Patient creates a case
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        # Doctor sees 0 cases before being assigned
        resp_before = await db_app_client.get("/api/v1/cases", headers=auth_header(d_token))
        assert resp_before.json()["total"] == 0

        # Doctor assigns themselves
        await db_app_client.patch(
            f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token)
        )

        resp_after = await db_app_client.get("/api/v1/cases", headers=auth_header(d_token))
        assert resp_after.json()["total"] == 1

    async def test_list_supports_pagination(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "ls04")
        for _ in range(3):
            await db_app_client.post("/api/v1/cases", headers=auth_header(token), json=case_payload())

        resp = await db_app_client.get(
            "/api/v1/cases", headers=auth_header(token), params={"page": 1, "page_size": 2}
        )
        body = resp.json()
        assert body["page_size"] == 2
        assert body["has_next"] is True
        assert len(body["items"]) == 2


# ================================================================== #
# GET /api/v1/cases/{case_id}
# ================================================================== #

class TestGetCase:

    async def test_patient_gets_own_case(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "gc01")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.get(f"/api/v1/cases/{case_id}", headers=auth_header(token))
        assert resp.status_code == 200
        assert resp.json()["id"] == case_id

    async def test_patient_cannot_see_other_patients_case(self, db_app_client: AsyncClient):
        token1, _ = await register_and_login_patient(db_app_client, "gc02a")
        token2, _ = await register_and_login_patient(db_app_client, "gc02b")

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token1), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        # patient2 tries to read patient1's case
        resp = await db_app_client.get(f"/api/v1/cases/{case_id}", headers=auth_header(token2))
        assert resp.status_code == 404  # enumeration-safe: 404 not 403

    async def test_unknown_case_id_returns_404(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "gc03")
        resp = await db_app_client.get(
            "/api/v1/cases/nonexistent-id", headers=auth_header(token)
        )
        assert resp.status_code == 404


# ================================================================== #
# PATCH /api/v1/cases/{case_id}
# ================================================================== #

class TestUpdateCase:

    async def test_patient_can_update_complaint(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "uc03")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}",
            headers=auth_header(token),
            json={"presenting_complaint": "Updated complaint"},
        )
        assert resp.status_code == 200
        assert resp.json()["presenting_complaint"] == "Updated complaint"

    async def test_assigned_doctor_can_update_complaint(self, db_app_client: AsyncClient, test_engine):
        """Assigned doctor is allowed to update the presenting complaint."""
        p_token, _ = await register_and_login_patient(db_app_client, "uc04p")
        d_token, _ = await register_and_login_doctor(db_app_client, "uc04d", test_engine)

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]
        await db_app_client.patch(f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token))

        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}",
            headers=auth_header(d_token),
            json={"presenting_complaint": "Doctor added detail"},
        )
        assert resp.status_code == 200

    async def test_doctor_can_update_clinical_status(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "uc05p")
        d_token, _ = await register_and_login_doctor(db_app_client, "uc05d", test_engine)

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]
        await db_app_client.patch(f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token))

        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}",
            headers=auth_header(d_token),
            json={"clinical_status": "resolved"},
        )
        assert resp.status_code == 200
        assert resp.json()["clinical_status"] == "resolved"

    async def test_patient_cannot_update_clinical_status(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "uc06")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}",
            headers=auth_header(token),
            json={"clinical_status": "resolved"},
        )
        assert resp.status_code == 403


# ================================================================== #
# DELETE /api/v1/cases/{case_id}
# ================================================================== #

class TestDeleteCase:

    async def test_patient_can_soft_cancel_case(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "dc01")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.delete(
            f"/api/v1/cases/{case_id}", headers=auth_header(token)
        )
        assert resp.status_code == 204

    async def test_after_cancel_case_is_still_accessible(self, db_app_client: AsyncClient):
        """After soft-cancel the case is still readable (medical history preserved)."""
        token, _ = await register_and_login_patient(db_app_client, "dc02")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        await db_app_client.delete(f"/api/v1/cases/{case_id}", headers=auth_header(token))
        detail = await db_app_client.get(f"/api/v1/cases/{case_id}", headers=auth_header(token))
        # Soft-delete preserves data but marks is_deleted; statuses are not auto-changed
        assert detail.status_code == 200
        body = detail.json()
        assert body["id"] == case_id


# ================================================================== #
# PATCH /api/v1/cases/{case_id}/assign
# ================================================================== #

class TestAssignDoctor:

    async def test_doctor_can_claim_case(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "as01p")
        d_token, d_id = await register_and_login_doctor(db_app_client, "as01d", test_engine)

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token)
        )
        assert resp.status_code == 200
        assert resp.json()["doctor_id"] == d_id

    async def test_same_doctor_assign_twice_is_idempotent(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "as02p")
        d_token, d_id = await register_and_login_doctor(db_app_client, "as02d", test_engine)

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        await db_app_client.patch(f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token))
        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token)
        )
        assert resp.status_code == 200
        assert resp.json()["doctor_id"] == d_id

    async def test_different_doctor_cannot_claim_assigned_case(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "as03p")
        d1_token, _ = await register_and_login_doctor(db_app_client, "as03d1", test_engine)
        d2_token, _ = await register_and_login_doctor(db_app_client, "as03d2", test_engine)

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        await db_app_client.patch(
            f"/api/v1/cases/{case_id}/assign", headers=auth_header(d1_token)
        )
        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}/assign", headers=auth_header(d2_token)
        )
        assert resp.status_code == 403

    async def test_patient_cannot_assign_doctor(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "as04")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}/assign", headers=auth_header(p_token)
        )
        assert resp.status_code == 403


# ================================================================== #
# GET /api/v1/cases/doctors/me/stats
# ================================================================== #

class TestDoctorStats:

    async def test_doctor_gets_stats(self, db_app_client: AsyncClient, test_engine):
        d_token, _ = await register_and_login_doctor(db_app_client, "st01", test_engine)
        resp = await db_app_client.get(
            "/api/v1/cases/doctors/me/stats", headers=auth_header(d_token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "today_cases" in body
        assert "pending_review" in body
        assert "total_assigned" in body

    async def test_patient_cannot_get_doctor_stats(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "st02")
        resp = await db_app_client.get(
            "/api/v1/cases/doctors/me/stats", headers=auth_header(token)
        )
        assert resp.status_code == 403


# ================================================================== #
# POST /api/v1/cases/doctor
# ================================================================== #

class TestDoctorCreateCase:
    """
    Tests for POST /api/v1/cases/doctor — doctor-initiated case creation.

    KEY BEHAVIOURS
    - patient_email is optional: omitted, null, or empty string all accepted.
    - When no email supplied a placeholder patient account is auto-created.
    - A valid email finds or creates the matching patient account.
    - An invalid (non-empty) email is rejected with 422.
    - Only approved doctors may call this endpoint.
    """

    BASE_PAYLOAD = {
        "patient_name": "Test Patient",
        "has_visible_lesion": True,
        "consultation_type": "new_complaint",
        "consent_research": False,
    }

    async def test_with_valid_email_creates_case(
        self, db_app_client: AsyncClient, test_engine
    ):
        d_token, _ = await register_and_login_doctor(db_app_client, "dc01", test_engine)
        resp = await db_app_client.post(
            "/api/v1/cases/doctor",
            headers=auth_header(d_token),
            json={**self.BASE_PAYLOAD, "patient_email": "newpatient_dc01@example.com"},
        )
        assert resp.status_code == 201, resp.text
        assert "id" in resp.json()

    async def test_with_empty_string_email_creates_case(
        self, db_app_client: AsyncClient, test_engine
    ):
        """Diagnose flow sends patient_email: '' — must be accepted after the fix."""
        d_token, _ = await register_and_login_doctor(db_app_client, "dc02", test_engine)
        resp = await db_app_client.post(
            "/api/v1/cases/doctor",
            headers=auth_header(d_token),
            json={**self.BASE_PAYLOAD, "patient_email": ""},
        )
        assert resp.status_code == 201, resp.text
        assert "id" in resp.json()

    async def test_with_missing_email_key_creates_case(
        self, db_app_client: AsyncClient, test_engine
    ):
        """Omitting patient_email entirely must also be accepted."""
        d_token, _ = await register_and_login_doctor(db_app_client, "dc03", test_engine)
        resp = await db_app_client.post(
            "/api/v1/cases/doctor",
            headers=auth_header(d_token),
            json=self.BASE_PAYLOAD,
        )
        assert resp.status_code == 201, resp.text
        assert "id" in resp.json()

    async def test_with_invalid_email_rejected(
        self, db_app_client: AsyncClient, test_engine
    ):
        """A non-empty, non-valid email must still be rejected with 422."""
        d_token, _ = await register_and_login_doctor(db_app_client, "dc04", test_engine)
        resp = await db_app_client.post(
            "/api/v1/cases/doctor",
            headers=auth_header(d_token),
            json={**self.BASE_PAYLOAD, "patient_email": "not-an-email"},
        )
        assert resp.status_code == 422

    async def test_patient_cannot_create_doctor_case(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "dc05")
        resp = await db_app_client.post(
            "/api/v1/cases/doctor",
            headers=auth_header(token),
            json={**self.BASE_PAYLOAD, "patient_email": "x@x.com"},
        )
        assert resp.status_code == 403


