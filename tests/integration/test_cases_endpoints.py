"""
tests/integration/test_cases_endpoints.py — Cases Endpoint Integration Tests
=============================================================================

Tests for the /api/v1/cases endpoints:
    POST   /api/v1/cases                    — create a case
    GET    /api/v1/cases                    — list cases (role-filtered)
    GET    /api/v1/cases/{id}               — get full case detail
    PATCH  /api/v1/cases/{id}               — update (consent, complaint, status)
    DELETE /api/v1/cases/{id}               — soft cancel
    PATCH  /api/v1/cases/{id}/assign        — doctor claims case
    GET    /api/v1/cases/doctors/me/stats   — doctor dashboard stats

KEY BEHAVIORS UNDER TEST
-------------------------
- Patients only see their own cases
- Doctors only see assigned cases
- CaseNotFoundException (404) returned for access-denied (not 403) — enumeration safe
- consent_given is immutable once True
- presenting_complaint is patient-only, clinical_status is doctor-only
- Soft-delete sets ai_status=failed, clinical_status=resolved
- Doctor assign is idempotent; different doctor → 403
"""

import pytest
from httpx import AsyncClient


# ================================================================== #
# Helpers
# ================================================================== #

async def register_and_login_patient(client: AsyncClient, suffix: str) -> tuple[str, str]:
    """Returns (access_token, user_id via profile)."""
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
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def register_and_login_doctor(client: AsyncClient, suffix: str) -> tuple[str, str]:
    """Returns (access_token, user_id via profile)."""
    await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Case Doctor {suffix}",
        "email": f"case_doctor_{suffix}@casestest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-CASE-{suffix}",
        "clinic_name": "Derm Clinic",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"case_doctor_{suffix}@casestest.com",
        "password": "DocPass9",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def case_payload(**overrides) -> dict:
    base = {
        "consultation_type": "new_complaint",
        "has_visible_lesion": True,
        "is_for_self": True,
        "presenting_complaint": "Red itchy rash on arm",
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
        assert body["consent_given"] is False
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

    async def test_doctor_cannot_create_case(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_doctor(db_app_client, "cr04")
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

    async def test_doctor_sees_only_assigned_cases(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "ls03")
        d_token, _ = await register_and_login_doctor(db_app_client, "ls03")

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

    async def test_patient_can_give_consent(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "uc01")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}",
            headers=auth_header(token),
            json={"consent_given": True},
        )
        assert resp.status_code == 200
        assert resp.json()["consent_given"] is True
        assert resp.json()["consent_given_at"] is not None

    async def test_consent_cannot_be_revoked(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "uc02")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        # Give consent
        await db_app_client.patch(
            f"/api/v1/cases/{case_id}",
            headers=auth_header(token),
            json={"consent_given": True},
        )

        # Try to revoke — should fail
        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}",
            headers=auth_header(token),
            json={"consent_given": False},
        )
        assert resp.status_code == 400

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

    async def test_doctor_cannot_update_complaint(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "uc04p")
        d_token, _ = await register_and_login_doctor(db_app_client, "uc04d")

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]
        await db_app_client.patch(f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token))

        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}",
            headers=auth_header(d_token),
            json={"presenting_complaint": "Doctor override"},
        )
        assert resp.status_code == 403

    async def test_doctor_can_update_clinical_status(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "uc05p")
        d_token, _ = await register_and_login_doctor(db_app_client, "uc05d")

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

    async def test_after_cancel_case_is_resolved(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "dc02")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        await db_app_client.delete(f"/api/v1/cases/{case_id}", headers=auth_header(token))
        detail = await db_app_client.get(f"/api/v1/cases/{case_id}", headers=auth_header(token))
        body = detail.json()
        assert body["ai_status"] == "failed"
        assert body["clinical_status"] == "resolved"


# ================================================================== #
# PATCH /api/v1/cases/{case_id}/assign
# ================================================================== #

class TestAssignDoctor:

    async def test_doctor_can_claim_case(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "as01p")
        d_token, d_id = await register_and_login_doctor(db_app_client, "as01d")

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token)
        )
        assert resp.status_code == 200
        assert resp.json()["doctor_id"] == d_id

    async def test_same_doctor_assign_twice_is_idempotent(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "as02p")
        d_token, d_id = await register_and_login_doctor(db_app_client, "as02d")

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

    async def test_different_doctor_cannot_claim_assigned_case(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "as03p")
        d1_token, _ = await register_and_login_doctor(db_app_client, "as03d1")
        d2_token, _ = await register_and_login_doctor(db_app_client, "as03d2")

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

    async def test_doctor_gets_stats(self, db_app_client: AsyncClient):
        _, d_token = await register_and_login_doctor(db_app_client, "st01")
        # d_token returned from helper is actually the token (second element is id here)
        # Fix: unpack correctly
        d_token, _ = await register_and_login_doctor(db_app_client, "st01x")
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
# POST /api/v1/cases/{case_id}/consent
# ================================================================== #

class TestConsentEndpoint:

    async def test_patient_can_give_consent(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "con01")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/consent", headers=auth_header(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["consent_given"] is True
        assert body["already_given"] is False
        assert body["consent_given_at"] is not None
        assert body["case_id"] == case_id

    async def test_consent_is_idempotent(self, db_app_client: AsyncClient):
        """Calling consent twice returns already_given=True on second call."""
        token, _ = await register_and_login_patient(db_app_client, "con02")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        first = await db_app_client.post(
            f"/api/v1/cases/{case_id}/consent", headers=auth_header(token)
        )
        second = await db_app_client.post(
            f"/api/v1/cases/{case_id}/consent", headers=auth_header(token)
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["already_given"] is False
        assert second.json()["already_given"] is True
        # Timestamp must not change on second call
        # Strip trailing Z/+00:00 before comparing — SQLite round-trips
        # timezone-aware datetimes as naive strings on the second read.
        first_ts = first.json()["consent_given_at"].rstrip("Z").rstrip("+00:00")
        second_ts = second.json()["consent_given_at"].rstrip("Z").rstrip("+00:00")
        assert first_ts == second_ts

    async def test_consent_is_reflected_in_case_detail(self, db_app_client: AsyncClient):
        """After giving consent, GET /cases/{id} shows consent_given=True."""
        token, _ = await register_and_login_patient(db_app_client, "con03")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]
        assert case_resp.json()["consent_given"] is False

        await db_app_client.post(
            f"/api/v1/cases/{case_id}/consent", headers=auth_header(token)
        )

        detail = await db_app_client.get(
            f"/api/v1/cases/{case_id}", headers=auth_header(token)
        )
        assert detail.json()["consent_given"] is True
        assert detail.json()["consent_given_at"] is not None

    async def test_other_patient_cannot_give_consent(self, db_app_client: AsyncClient):
        """Another patient cannot give consent for someone else's case."""
        token_a, _ = await register_and_login_patient(db_app_client, "con04a")
        token_b, _ = await register_and_login_patient(db_app_client, "con04b")

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token_a), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/consent", headers=auth_header(token_b)
        )
        assert resp.status_code == 404

    async def test_doctor_cannot_give_patient_consent(self, db_app_client: AsyncClient):
        """Doctors cannot call the consent endpoint."""
        p_token, _ = await register_and_login_patient(db_app_client, "con05p")
        d_token, _ = await register_and_login_doctor(db_app_client, "con05d")

        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/consent", headers=auth_header(d_token)
        )
        assert resp.status_code == 403

    async def test_unauthenticated_consent_returns_401(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "con06")
        case_resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token), json=case_payload()
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.post(f"/api/v1/cases/{case_id}/consent")
        assert resp.status_code == 401

    async def test_consent_for_nonexistent_case_returns_404(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "con07")
        resp = await db_app_client.post(
            "/api/v1/cases/nonexistent-case-id/consent", headers=auth_header(token)
        )
        assert resp.status_code == 404
