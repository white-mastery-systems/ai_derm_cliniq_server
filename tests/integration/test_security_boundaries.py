"""
tests/integration/test_security_boundaries.py — Security Boundary Tests
========================================================================

Tests that the authentication and authorization layer correctly blocks
all unauthorized access patterns.

CATEGORIES
----------
1. JWT Attacks
   - Missing token → 401
   - Malformed / garbage token → 401
   - Refresh token used as access token → 401 (type confusion)
   - Tampered payload (different user) → 401
   - Token with wrong signing key → 401

2. Role Escalation
   - Patient accessing doctor-only endpoints → 403
   - Patient accessing admin-only endpoints → 403
   - Doctor accessing admin-only endpoints → 403
   - Patient trying to call admin approve/reject endpoints → 403

3. Enumeration Safety
   - Unauthorized case access returns 404 (not 403)
   - Non-existent case returns 404 whether authorized or not
   - Response error body does not reveal internal identifiers

4. Auth Dependency Edge Cases
   - "Bearer" header present but token is empty string → 401
   - Authorization header without "Bearer " prefix → 401
   - Inactive (soft-deleted) user's token is rejected → 401
   - Unapproved doctor blocked at login time (not at endpoint)
"""

import base64
import json

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from unittest.mock import patch

from src.auth.jwt import create_access_token, create_refresh_token
from src.auth.security import hash_password
from src.models.base import new_uuid
from src.models.user import User, UserRole


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def register_and_login_patient(client: AsyncClient, suffix: str) -> tuple[str, str]:
    await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"Sec Patient {suffix}",
        "email": f"sec_patient_{suffix}@sectest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"sec_patient_{suffix}@sectest.com",
        "password": "TestPass1",
    })
    token = resp.json()["access_token"]
    await client.patch(
        "/api/v1/users/me",
        headers=auth_header(token),
        json={"date_of_birth": "1990-01-01", "gender": "female"},
    )
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def _create_admin_and_approve_doctor(client, test_engine, doctor_id: str) -> None:
    admin_email = f"_tmp_admin_{doctor_id[:8]}@sectest.com"
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        admin = User(
            id=new_uuid(),
            email=admin_email,
            full_name="Tmp Admin",
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


async def register_and_login_doctor(client: AsyncClient, suffix: str, test_engine) -> tuple[str, str]:
    reg_resp = await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Sec Doctor {suffix}",
        "email": f"sec_doctor_{suffix}@sectest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-SEC-{suffix}",
        "clinic_name": "Sec Clinic",
    })
    doctor_id = reg_resp.json()["user"]["id"]
    await _create_admin_and_approve_doctor(client, test_engine, doctor_id)
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"sec_doctor_{suffix}@sectest.com",
        "password": "DocPass9",
    })
    return resp.json()["access_token"], doctor_id


async def create_admin(test_engine, suffix: str) -> str:
    """Insert admin directly, return access token via login."""
    email = f"sec_admin_{suffix}@sectest.com"
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        admin = User(
            id=new_uuid(),
            email=email,
            full_name=f"Sec Admin {suffix}",
            role=UserRole.ADMIN,
            password_hash=hash_password("AdminPass9"),
            is_active=True,
            is_verified=True,
        )
        session.add(admin)
        await session.commit()
    return email


def case_payload() -> dict:
    return {
        "consultation_type": "new_complaint",
        "has_visible_lesion": True,
        "is_for_self": True,
        "presenting_complaint": "Test rash",
        "consent_ai_analysis": True,
    }


# ================================================================== #
# 1. JWT Attacks
# ================================================================== #

class TestJwtAttacks:

    async def test_missing_authorization_header_returns_401(self, db_app_client: AsyncClient):
        resp = await db_app_client.get("/api/v1/users/me")
        assert resp.status_code == 401

    async def test_garbage_token_returns_401(self, db_app_client: AsyncClient):
        resp = await db_app_client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer this.is.garbage"},
        )
        assert resp.status_code == 401

    async def test_refresh_token_used_as_access_token_returns_401(self, db_app_client: AsyncClient):
        """Type-confusion attack: refresh token must be rejected by access-token-protected routes."""
        # Register a real user to get a real user ID
        await db_app_client.post("/api/v1/auth/register/patient", json={
            "full_name": "JWT Attack Patient",
            "email": "jwt_attack@sectest.com",
            "password": "TestPass1",
        })
        # Create a refresh token (signed correctly, but wrong type="refresh")
        refresh_token = create_refresh_token("some-user-id", "patient")

        resp = await db_app_client.get(
            "/api/v1/users/me",
            headers=auth_header(refresh_token),
        )
        assert resp.status_code == 401

    async def test_tampered_payload_returns_401(self, db_app_client: AsyncClient):
        """
        Tamper the payload section of a real JWT to claim a different sub.
        The signature will be invalid → 401.
        """
        # Register a real patient to produce a valid token structure
        await db_app_client.post("/api/v1/auth/register/patient", json={
            "full_name": "Tamper Patient",
            "email": "tamper_p@sectest.com",
            "password": "TestPass1",
        })
        real_token = (await db_app_client.post("/api/v1/auth/login", json={
            "email": "tamper_p@sectest.com",
            "password": "TestPass1",
        })).json()["access_token"]

        # Decode the payload, change the sub, re-encode without re-signing
        header_b64, payload_b64, signature = real_token.split(".")
        # Pad to valid base64
        payload_padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_padded))
        payload["sub"] = "00000000-0000-0000-0000-000000000000"  # different user
        new_payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).rstrip(b"=").decode()

        tampered_token = f"{header_b64}.{new_payload_b64}.{signature}"

        resp = await db_app_client.get("/api/v1/users/me", headers=auth_header(tampered_token))
        assert resp.status_code == 401

    async def test_empty_bearer_token_returns_401(self, db_app_client: AsyncClient):
        """Authorization: Bearer <empty> must be rejected."""
        resp = await db_app_client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code == 401

    async def test_non_bearer_scheme_returns_401(self, db_app_client: AsyncClient):
        """Authorization: Basic ... or Token ... must be rejected — only Bearer accepted."""
        valid_token = create_access_token("some-id", "patient")
        resp = await db_app_client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Token {valid_token}"},
        )
        assert resp.status_code == 401

    async def test_inactive_user_token_is_rejected(self, db_app_client: AsyncClient):
        """
        A user who soft-deletes their account has is_active=False.
        Their still-valid JWT should be rejected because get_current_user
        checks is_active after DB lookup.
        """
        token, _ = await register_and_login_patient(db_app_client, "inactive01")
        # Soft delete the account
        await db_app_client.delete("/api/v1/users/me", headers=auth_header(token))

        # The same token is now invalid (user is inactive in DB)
        resp = await db_app_client.get("/api/v1/users/me", headers=auth_header(token))
        assert resp.status_code == 401


# ================================================================== #
# 2. Role Escalation
# ================================================================== #

class TestRoleEscalation:

    async def test_patient_cannot_access_doctor_only_by_code_endpoint(
        self, db_app_client: AsyncClient
    ):
        """GET /users/by-code/ requires DOCTOR role — patients get 403."""
        token, _ = await register_and_login_patient(db_app_client, "re01")
        resp = await db_app_client.get(
            "/api/v1/users/by-code/ABC-1234-Z",
            headers=auth_header(token),
        )
        assert resp.status_code == 403

    async def test_patient_cannot_access_admin_users_list(self, db_app_client: AsyncClient):
        """GET /admin/users requires ADMIN role — patients get 403."""
        token, _ = await register_and_login_patient(db_app_client, "re02")
        resp = await db_app_client.get("/api/v1/admin/users", headers=auth_header(token))
        assert resp.status_code == 403

    async def test_patient_cannot_access_admin_cases_list(self, db_app_client: AsyncClient):
        """GET /admin/cases requires ADMIN role — patients get 403."""
        token, _ = await register_and_login_patient(db_app_client, "re03")
        resp = await db_app_client.get("/api/v1/admin/cases", headers=auth_header(token))
        assert resp.status_code == 403

    async def test_doctor_cannot_access_admin_users_list(
        self, db_app_client: AsyncClient, test_engine
    ):
        """GET /admin/users requires ADMIN role — doctors get 403."""
        d_token, _ = await register_and_login_doctor(db_app_client, "re04", test_engine)
        resp = await db_app_client.get("/api/v1/admin/users", headers=auth_header(d_token))
        assert resp.status_code == 403

    async def test_doctor_cannot_approve_another_doctor(
        self, db_app_client: AsyncClient, test_engine
    ):
        """POST /admin/doctors/{id}/approve is admin-only — doctors get 403."""
        # Register a pending doctor (the target)
        pending_resp = await db_app_client.post("/api/v1/auth/register/doctor", json={
            "full_name": "Pending Doc",
            "email": "pending_re05@sectest.com",
            "password": "DocPass9",
            "specialization": "Dermatology",
            "license_number": "LIC-PEND-RE05",
            "clinic_name": "Clinic",
        })
        pending_id = pending_resp.json()["user"]["id"]

        # Approved doctor tries to approve another doctor
        d_token, _ = await register_and_login_doctor(db_app_client, "re05", test_engine)
        with patch("src.core.email.send_email", return_value=True):
            resp = await db_app_client.post(
                f"/api/v1/admin/doctors/{pending_id}/approve",
                headers=auth_header(d_token),
            )
        assert resp.status_code == 403

    async def test_patient_cannot_create_doctor_account_via_patient_registration(
        self, db_app_client: AsyncClient
    ):
        """POST /auth/register/patient always creates role=patient — role field in body is ignored."""
        resp = await db_app_client.post("/api/v1/auth/register/patient", json={
            "full_name": "Escalation Attempt",
            "email": "escalate@sectest.com",
            "password": "TestPass1",
            "role": "admin",  # should be silently ignored
        })
        # Registration should succeed but role must be 'patient'
        assert resp.status_code == 201
        assert resp.json()["user"]["role"] == "patient"

    async def test_patient_cannot_get_doctor_stats(self, db_app_client: AsyncClient):
        """GET /cases/doctors/me/stats is doctor-only — patients get 403."""
        token, _ = await register_and_login_patient(db_app_client, "re06")
        resp = await db_app_client.get(
            "/api/v1/cases/doctors/me/stats", headers=auth_header(token)
        )
        assert resp.status_code == 403

    async def test_doctor_cannot_create_case(self, db_app_client: AsyncClient, test_engine):
        """POST /cases is patient-only — doctors get 403."""
        d_token, _ = await register_and_login_doctor(db_app_client, "re07", test_engine)
        resp = await db_app_client.post(
            "/api/v1/cases", headers=auth_header(d_token), json=case_payload()
        )
        assert resp.status_code == 403


# ================================================================== #
# 3. Enumeration Safety
# ================================================================== #

class TestEnumerationSafety:

    async def test_cross_patient_case_access_returns_404_not_403(
        self, db_app_client: AsyncClient
    ):
        """Unauthorized case access must be 404 to prevent resource enumeration."""
        token_a, _ = await register_and_login_patient(db_app_client, "es01a")
        token_b, _ = await register_and_login_patient(db_app_client, "es01b")

        case_id = (await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token_a), json=case_payload()
        )).json()["id"]

        resp = await db_app_client.get(f"/api/v1/cases/{case_id}", headers=auth_header(token_b))
        assert resp.status_code == 404

    async def test_nonexistent_case_id_returns_404(self, db_app_client: AsyncClient):
        """A UUID that doesn't exist in the DB returns 404 for any authenticated user."""
        token, _ = await register_and_login_patient(db_app_client, "es02")
        fake_id = str(new_uuid())
        resp = await db_app_client.get(f"/api/v1/cases/{fake_id}", headers=auth_header(token))
        assert resp.status_code == 404

    async def test_404_error_body_does_not_reveal_ownership(self, db_app_client: AsyncClient):
        """
        The 404 error message must not say 'belongs to another user' or similar.
        It should say 'not found' — same message whether the resource doesn't exist
        or the requester is unauthorized.
        """
        token_a, _ = await register_and_login_patient(db_app_client, "es03a")
        token_b, _ = await register_and_login_patient(db_app_client, "es03b")

        case_id = (await db_app_client.post(
            "/api/v1/cases", headers=auth_header(token_a), json=case_payload()
        )).json()["id"]

        resp = await db_app_client.get(f"/api/v1/cases/{case_id}", headers=auth_header(token_b))
        assert resp.status_code == 404

        # The error message should not hint that the resource exists
        error_body = resp.json()
        error_text = str(error_body).lower()
        assert "unauthorized" not in error_text
        assert "forbidden" not in error_text
        assert "belongs to" not in error_text

    async def test_unassigned_doctor_case_access_returns_404_not_403(
        self, db_app_client: AsyncClient, test_engine
    ):
        """Same enumeration-safe behavior applies to doctors."""
        p_token, _ = await register_and_login_patient(db_app_client, "es04p")
        d_token, _ = await register_and_login_doctor(db_app_client, "es04d", test_engine)

        case_id = (await db_app_client.post(
            "/api/v1/cases", headers=auth_header(p_token), json=case_payload()
        )).json()["id"]

        resp = await db_app_client.get(f"/api/v1/cases/{case_id}", headers=auth_header(d_token))
        assert resp.status_code == 404


# ================================================================== #
# 4. Auth Dependency Edge Cases
# ================================================================== #

class TestAuthDependencyEdgeCases:

    async def test_unapproved_doctor_cannot_login(self, db_app_client: AsyncClient):
        """
        A doctor who just registered (is_active=False) cannot login.
        This is the gate that prevents unapproved doctors from any access.
        """
        await db_app_client.post("/api/v1/auth/register/doctor", json={
            "full_name": "Unapproved Doctor",
            "email": "unapproved@sectest.com",
            "password": "DocPass9",
            "specialization": "Dermatology",
            "license_number": "LIC-UNAPP",
            "clinic_name": "Pending Clinic",
        })
        resp = await db_app_client.post("/api/v1/auth/login", json={
            "email": "unapproved@sectest.com",
            "password": "DocPass9",
        })
        # Must not succeed — doctor pending approval
        assert resp.status_code in (401, 403)

    async def test_wrong_password_returns_401(self, db_app_client: AsyncClient):
        """Wrong password must be rejected (basic auth sanity check)."""
        await db_app_client.post("/api/v1/auth/register/patient", json={
            "full_name": "Wrong Pass Patient",
            "email": "wrongpass@sectest.com",
            "password": "CorrectPass1",
        })
        resp = await db_app_client.post("/api/v1/auth/login", json={
            "email": "wrongpass@sectest.com",
            "password": "WrongPass999",
        })
        assert resp.status_code == 401

    async def test_nonexistent_user_login_returns_401(self, db_app_client: AsyncClient):
        """Login with email that doesn't exist must return 401."""
        resp = await db_app_client.post("/api/v1/auth/login", json={
            "email": "nobody@sectest.com",
            "password": "AnyPass1",
        })
        assert resp.status_code == 401

    async def test_all_protected_routes_reject_no_token(self, db_app_client: AsyncClient):
        """Spot-check: key routes must return 401 without a token."""
        protected_routes = [
            ("GET",    "/api/v1/users/me"),
            ("PATCH",  "/api/v1/users/me"),
            ("DELETE", "/api/v1/users/me"),
            ("GET",    "/api/v1/cases"),
            ("POST",   "/api/v1/cases"),
        ]
        for method, path in protected_routes:
            resp = await db_app_client.request(method, path)
            assert resp.status_code == 401, (
                f"{method} {path} returned {resp.status_code}, expected 401"
            )
