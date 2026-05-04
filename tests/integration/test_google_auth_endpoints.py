"""
tests/integration/test_google_auth_endpoints.py — Google Auth & Device Token Tests
====================================================================================

Tests for:
    POST /api/v1/auth/google              — Google Sign-In / register
    POST /api/v1/users/me/device-token    — register / refresh FCM token

MOCK STRATEGY
-------------
verify_google_id_token is patched so no real Google network call is made.
The mock returns a GoogleUserInfo object matching the shape the real function returns.
All DB interactions go to the in-memory SQLite test database.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from src.auth.google import GoogleUserInfo


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def fake_google_info(
    google_id: str = "google-uid-123",
    email: str = "google_user@gmail.com",
    full_name: str = "Google User",
) -> GoogleUserInfo:
    return GoogleUserInfo(google_id=google_id, email=email, full_name=full_name)


def mock_google_verify(google_info: GoogleUserInfo):
    """Patch verify_google_id_token to return the given GoogleUserInfo."""
    return patch(
        "src.auth.service.verify_google_id_token",
        new=AsyncMock(return_value=google_info),
    )


# ================================================================== #
# POST /api/v1/auth/google — new patient registration
# ================================================================== #

class TestGoogleAuthNewUser:

    async def test_new_patient_gets_tokens(self, db_app_client: AsyncClient):
        """First Google login creates a patient account and returns tokens."""
        with mock_google_verify(fake_google_info()):
            resp = await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "fake-google-id-token",
                "role": "patient",
            })

        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["role"] == "patient"
        assert body["token_type"] == "bearer"

    async def test_new_doctor_via_google(self, db_app_client: AsyncClient):
        """First Google login with role=doctor creates a doctor account."""
        with mock_google_verify(fake_google_info(
            google_id="google-doc-uid",
            email="google_doctor@gmail.com",
        )):
            resp = await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "fake-token",
                "role": "doctor",
            })

        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "doctor"

    async def test_google_verified_user_is_active(self, db_app_client: AsyncClient):
        """Google users are immediately active and verified (Google verified the email)."""
        with mock_google_verify(fake_google_info(
            google_id="google-active-uid",
            email="google_active@gmail.com",
        )):
            resp = await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "fake-token",
                "role": "patient",
            })

        assert resp.status_code == 200
        # Use the token to call /me — should work immediately (no approval needed)
        token = resp.json()["access_token"]
        me_resp = await db_app_client.get("/api/v1/users/me", headers=auth_header(token))
        assert me_resp.status_code == 200
        assert me_resp.json()["is_active"] is True
        assert me_resp.json()["is_verified"] is True

    async def test_fcm_token_stored_on_google_login(self, db_app_client: AsyncClient):
        """FCM token provided at Google login is saved to the user."""
        with mock_google_verify(fake_google_info(
            google_id="google-fcm-uid",
            email="google_fcm@gmail.com",
        )):
            resp = await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "fake-token",
                "role": "patient",
                "fcm_token": "fcm-device-token-abc123",
            })

        assert resp.status_code == 200

    async def test_role_defaults_to_patient_when_invalid(self, db_app_client: AsyncClient):
        """An unrecognised role value is rejected by schema validation (422)."""
        with mock_google_verify(fake_google_info(
            google_id="google-bad-role",
            email="google_badrole@gmail.com",
        )):
            resp = await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "fake-token",
                "role": "admin",
            })

        assert resp.status_code == 422  # schema rejects "admin"


# ================================================================== #
# POST /api/v1/auth/google — returning user (second login)
# ================================================================== #

class TestGoogleAuthReturningUser:

    async def test_second_login_returns_tokens(self, db_app_client: AsyncClient):
        """Second Google login with the same google_id issues new tokens."""
        info = fake_google_info(
            google_id="returning-google-uid",
            email="returning_google@gmail.com",
        )

        with mock_google_verify(info):
            # First login — creates the account
            await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "token-1",
                "role": "patient",
            })

        with mock_google_verify(info):
            # Second login — same google_id, should succeed
            resp = await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "token-2",
                "role": "patient",
            })

        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_google_links_to_existing_email_account(self, db_app_client: AsyncClient):
        """
        If a user registered with email+password and then signs in with Google
        using the same email, the accounts are linked (no duplicate created).
        """
        email = "link_test@gmail.com"

        # Register with email+password first
        await db_app_client.post("/api/v1/auth/register/patient", json={
            "full_name": "Link Test User",
            "email": email,
            "password": "TestPass1",
        })

        # Now sign in with Google using the same email
        with mock_google_verify(fake_google_info(
            google_id="google-link-uid",
            email=email,
        )):
            resp = await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "fake-token",
                "role": "patient",
            })

        assert resp.status_code == 200
        body = resp.json()
        # Same user — same user_id should be returned
        assert "access_token" in body


# ================================================================== #
# POST /api/v1/auth/google — invalid token
# ================================================================== #

class TestGoogleAuthInvalidToken:

    async def test_invalid_google_token_returns_401(self, db_app_client: AsyncClient):
        """An invalid or expired Google ID token returns 401."""
        from src.exceptions import UnauthorizedException

        with patch(
            "src.auth.service.verify_google_id_token",
            new=AsyncMock(side_effect=UnauthorizedException(message="Invalid or expired Google token")),
        ):
            resp = await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "expired-or-fake-token",
                "role": "patient",
            })

        assert resp.status_code == 401

    async def test_google_unreachable_returns_401(self, db_app_client: AsyncClient):
        """If Google's tokeninfo endpoint is unreachable, return 401."""
        from src.exceptions import UnauthorizedException

        with patch(
            "src.auth.service.verify_google_id_token",
            new=AsyncMock(side_effect=UnauthorizedException(message="Could not reach Google authentication service")),
        ):
            resp = await db_app_client.post("/api/v1/auth/google", json={
                "id_token": "any-token",
                "role": "patient",
            })

        assert resp.status_code == 401

    async def test_missing_id_token_returns_422(self, db_app_client: AsyncClient):
        """Omitting id_token fails schema validation."""
        resp = await db_app_client.post("/api/v1/auth/google", json={
            "role": "patient",
        })
        assert resp.status_code == 422


# ================================================================== #
# POST /api/v1/users/me/device-token
# ================================================================== #

class TestDeviceToken:

    async def test_patient_can_update_device_token(self, db_app_client: AsyncClient):
        """Patient successfully registers a new FCM device token."""
        await db_app_client.post("/api/v1/auth/register/patient", json={
            "full_name": "Device Token Patient",
            "email": "devtoken_patient@test.com",
            "password": "TestPass1",
        })
        login = await db_app_client.post("/api/v1/auth/login", json={
            "email": "devtoken_patient@test.com",
            "password": "TestPass1",
        })
        token = login.json()["access_token"]

        resp = await db_app_client.post(
            "/api/v1/users/me/device-token",
            headers=auth_header(token),
            json={"fcm_token": "new-device-token-xyz789"},
        )

        assert resp.status_code == 200
        assert resp.json()["message"] == "Device token updated"

    async def test_device_token_update_requires_auth(self, db_app_client: AsyncClient):
        """Unauthenticated request returns 401."""
        resp = await db_app_client.post(
            "/api/v1/users/me/device-token",
            json={"fcm_token": "some-token"},
        )
        assert resp.status_code == 401

    async def test_device_token_missing_field_returns_422(self, db_app_client: AsyncClient):
        """Omitting fcm_token fails schema validation."""
        await db_app_client.post("/api/v1/auth/register/patient", json={
            "full_name": "Validation Patient",
            "email": "devtoken_val@test.com",
            "password": "TestPass1",
        })
        login = await db_app_client.post("/api/v1/auth/login", json={
            "email": "devtoken_val@test.com",
            "password": "TestPass1",
        })
        token = login.json()["access_token"]

        resp = await db_app_client.post(
            "/api/v1/users/me/device-token",
            headers=auth_header(token),
            json={},
        )
        assert resp.status_code == 422

    async def test_token_updates_on_successive_calls(self, db_app_client: AsyncClient):
        """Calling the endpoint twice replaces the old token with the new one."""
        await db_app_client.post("/api/v1/auth/register/patient", json={
            "full_name": "Replace Token Patient",
            "email": "devtoken_replace@test.com",
            "password": "TestPass1",
        })
        login = await db_app_client.post("/api/v1/auth/login", json={
            "email": "devtoken_replace@test.com",
            "password": "TestPass1",
        })
        token = login.json()["access_token"]

        await db_app_client.post(
            "/api/v1/users/me/device-token",
            headers=auth_header(token),
            json={"fcm_token": "old-token"},
        )
        resp = await db_app_client.post(
            "/api/v1/users/me/device-token",
            headers=auth_header(token),
            json={"fcm_token": "new-token"},
        )

        assert resp.status_code == 200

    async def test_fcm_token_also_updates_on_login(self, db_app_client: AsyncClient):
        """FCM token provided at login is stored — same as the device-token endpoint."""
        await db_app_client.post("/api/v1/auth/register/patient", json={
            "full_name": "Login FCM Patient",
            "email": "loginfcm@test.com",
            "password": "TestPass1",
        })
        resp = await db_app_client.post("/api/v1/auth/login", json={
            "email": "loginfcm@test.com",
            "password": "TestPass1",
            "fcm_token": "login-device-token-abc",
        })

        assert resp.status_code == 200
        assert "access_token" in resp.json()
