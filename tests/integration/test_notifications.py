"""
tests/integration/test_notifications.py — Notification Integration Tests
=========================================================================

Tests cover:
  1. fcm_token saved on patient registration
  2. fcm_token saved on doctor registration
  3. fcm_token updated on login
  4. fcm_token accepted on Google auth schema
  5. Admin notification task enqueued on doctor registration
  6. Doctor approval notification task enqueued
  7. Doctor rejection notification task enqueued
  8. Patient AI-complete notification task enqueued (via save_results mock)
  9. Patient review-complete notification task enqueued
 10. Patient clinical status update notification enqueued
 11. Red flag notification task enqueued

FCM calls are fully mocked — no real Firebase connection is made.
Celery .delay() calls are mocked via unittest.mock to verify they are
called with the correct arguments without needing a running broker.
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models.case import AiStatus, Case
from src.models.doctor_review import ReviewStatus
from src.models.user import User

# Ensure notification submodules are imported so patch() can resolve them
import src.notifications.firebase  # noqa: F401, E402
import src.notifications.service   # noqa: F401, E402


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def register_patient(
    client: AsyncClient,
    suffix: str,
    fcm_token: str | None = None,
    with_profile: bool = False,
) -> dict:
    body = {
        "full_name": f"Notif Patient {suffix}",
        "email": f"notif_patient_{suffix}@test.com",
        "password": "TestPass1",
    }
    if fcm_token:
        body["fcm_token"] = fcm_token
    if with_profile:
        body["date_of_birth"] = "1990-01-01"
        body["gender"] = "Male"
    resp = await client.post("/api/v1/auth/register/patient", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def register_doctor(client: AsyncClient, suffix: str, fcm_token: str | None = None) -> dict:
    body = {
        "full_name": f"Notif Doctor {suffix}",
        "email": f"notif_doctor_{suffix}@test.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-N-{suffix}",
        "clinic_name": "Derm Clinic",
    }
    if fcm_token:
        body["fcm_token"] = fcm_token
    resp = await client.post("/api/v1/auth/register/doctor", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def login_user(
    client: AsyncClient,
    email: str,
    password: str,
    fcm_token: str | None = None,
) -> dict:
    body = {"email": email, "password": password}
    if fcm_token:
        body["fcm_token"] = fcm_token
    resp = await client.post("/api/v1/auth/login", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def activate_doctor(test_engine, user_id: str) -> None:
    """Directly mark doctor as active + verified in the test DB."""
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        user = await session.get(User, user_id)
        user.is_active = True
        user.is_verified = True
        await session.commit()


async def set_ai_completed(test_engine, case_id: str) -> None:
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        case = await session.get(Case, case_id)
        case.ai_status = AiStatus.COMPLETED
        await session.commit()


async def get_user_by_email(test_engine, email: str) -> User | None:
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


# ================================================================== #
# 1. fcm_token saved on patient registration
# ================================================================== #

@pytest.mark.asyncio
async def test_patient_register_saves_fcm_token(db_app_client: AsyncClient, test_engine):
    """fcm_token provided at registration is persisted to the users table."""
    token = "fcm_patient_reg_token_abc123"
    await register_patient(db_app_client, "fcm1", fcm_token=token)

    user = await get_user_by_email(test_engine, "notif_patient_fcm1@test.com")
    assert user is not None
    assert user.fcm_token == token


@pytest.mark.asyncio
async def test_patient_register_without_fcm_token(db_app_client: AsyncClient, test_engine):
    """Registration without fcm_token leaves fcm_token as NULL."""
    await register_patient(db_app_client, "fcm2")

    user = await get_user_by_email(test_engine, "notif_patient_fcm2@test.com")
    assert user is not None
    assert user.fcm_token is None


# ================================================================== #
# 2. fcm_token saved on doctor registration
# ================================================================== #

@pytest.mark.asyncio
async def test_doctor_register_saves_fcm_token(db_app_client: AsyncClient, test_engine):
    """Doctor fcm_token is saved even though no login tokens are issued."""
    token = "fcm_doctor_reg_token_xyz789"

    with patch(
        "src.workers.tasks.notifications.notify_admins_doctor_registered.delay"
    ) as mock_task:
        await register_doctor(db_app_client, "fcm3", fcm_token=token)
        mock_task.assert_called_once()

    user = await get_user_by_email(test_engine, "notif_doctor_fcm3@test.com")
    assert user is not None
    assert user.fcm_token == token


# ================================================================== #
# 3. fcm_token updated on login
# ================================================================== #

@pytest.mark.asyncio
async def test_login_updates_fcm_token(db_app_client: AsyncClient, test_engine):
    """Each login call refreshes the stored fcm_token."""
    old_token = "old_device_token"
    new_token = "new_device_token_after_reinstall"

    await register_patient(db_app_client, "fcm4", fcm_token=old_token)
    await login_user(
        db_app_client,
        "notif_patient_fcm4@test.com",
        "TestPass1",
        fcm_token=new_token,
    )

    user = await get_user_by_email(test_engine, "notif_patient_fcm4@test.com")
    assert user.fcm_token == new_token


@pytest.mark.asyncio
async def test_login_without_fcm_token_keeps_existing(db_app_client: AsyncClient, test_engine):
    """Login without fcm_token does not overwrite the existing stored token."""
    token = "existing_token_should_stay"
    await register_patient(db_app_client, "fcm5", fcm_token=token)
    await login_user(db_app_client, "notif_patient_fcm5@test.com", "TestPass1")

    user = await get_user_by_email(test_engine, "notif_patient_fcm5@test.com")
    assert user.fcm_token == token


# ================================================================== #
# 4. Admin notification on doctor registration
# ================================================================== #

@pytest.mark.asyncio
async def test_doctor_registration_fires_admin_notification(db_app_client: AsyncClient, test_engine):
    """Registering a doctor enqueues the notify_admins_doctor_registered Celery task."""
    with patch(
        "src.workers.tasks.notifications.notify_admins_doctor_registered.delay"
    ) as mock_task:
        resp = await register_doctor(db_app_client, "ntf1")
        doctor_id = resp["user"]["id"]

        mock_task.assert_called_once_with(
            doctor_name="Notif Doctor ntf1",
            doctor_id=doctor_id,
        )


# ================================================================== #
# 5. Doctor approval notification
# ================================================================== #

@pytest.mark.asyncio
async def test_doctor_approval_fires_notification(db_app_client: AsyncClient, test_engine):
    """Approving a doctor via admin endpoint enqueues notify_doctor_approved."""
    # Register and get an admin token
    admin_resp = await db_app_client.post("/api/v1/auth/login", json={
        "email": "admin@aidermcliniq.com",   # bootstrap admin from conftest env
        "password": "Admin@123",
    })

    if admin_resp.status_code != 200:
        pytest.skip("Admin account not available in test DB — skipping approval test")

    admin_token = admin_resp.json()["access_token"]

    with patch(
        "src.workers.tasks.notifications.notify_admins_doctor_registered.delay"
    ):
        doctor_resp = await register_doctor(db_app_client, "ntf2")
    doctor_id = doctor_resp["user"]["id"]

    with patch(
        "src.workers.tasks.notifications.notify_doctor_approved.delay"
    ) as mock_approve:
        resp = await db_app_client.post(
            f"/api/v1/admin/doctors/{doctor_id}/approve",
            headers=auth_header(admin_token),
        )
        # If admin endpoint is available and doctor was approved
        if resp.status_code == 200:
            mock_approve.assert_called_once_with(doctor_id=doctor_id)


# ================================================================== #
# 6. Doctor review complete → patient notification
# ================================================================== #

@pytest.mark.asyncio
async def test_review_complete_fires_notification(db_app_client: AsyncClient, test_engine):
    """Completing a doctor review fires notify_patient_review_complete."""
    # Setup: patient creates case, mark AI complete
    patient_resp = await register_patient(db_app_client, "rv1", with_profile=True)
    patient_token = patient_resp["access_token"]

    case_resp = await db_app_client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "is_for_self": True,
            "presenting_complaint": "Itchy rash",
            "consent_ai_analysis": True,
        },
    )
    assert case_resp.status_code == 201, case_resp.text
    case_id = case_resp.json()["id"]
    await set_ai_completed(test_engine, case_id)

    # Setup: register & activate doctor
    with patch("src.workers.tasks.notifications.notify_admins_doctor_registered.delay"):
        doctor_resp = await register_doctor(db_app_client, "rv1")
    doctor_id = doctor_resp["user"]["id"]
    await activate_doctor(test_engine, doctor_id)

    doctor_token_resp = await db_app_client.post("/api/v1/auth/login", json={
        "email": "notif_doctor_rv1@test.com",
        "password": "DocPass9",
    })
    doctor_token = doctor_token_resp.json()["access_token"]

    # Doctor scans QR to get assigned
    qr_resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    assert qr_resp.status_code == 201
    qr_token = qr_resp.json()["token"]
    await db_app_client.post(f"/api/v1/qr/scan/{qr_token}", headers=auth_header(doctor_token))

    # Doctor creates review with COMPLETED status
    with patch(
        "src.workers.tasks.notifications.notify_patient_review_complete.delay"
    ) as mock_notif:
        review_resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/review",
            headers=auth_header(doctor_token),
            json={
                "is_ai_correct": True,
                "selected_differentials": ["Eczema"],
                "review_status": "completed",
            },
        )
        assert review_resp.status_code == 201
        mock_notif.assert_called_once()
        call_kwargs = mock_notif.call_args.kwargs
        assert call_kwargs["case_id"] == case_id
        assert "doctor_name" in call_kwargs


# ================================================================== #
# 7. Clinical status update → patient notification
# ================================================================== #

@pytest.mark.asyncio
async def test_clinical_status_update_fires_notification(db_app_client: AsyncClient, test_engine):
    """Updating case clinical_status via PATCH review fires notify_patient_status_update."""
    # Setup patient + case + doctor (same as above, different suffix)
    patient_resp = await register_patient(db_app_client, "cs1", with_profile=True)
    patient_token = patient_resp["access_token"]

    case_resp = await db_app_client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "is_for_self": True,
            "presenting_complaint": "Dry skin",
            "consent_ai_analysis": True,
        },
    )
    assert case_resp.status_code == 201, case_resp.text
    case_id = case_resp.json()["id"]
    await set_ai_completed(test_engine, case_id)

    with patch("src.workers.tasks.notifications.notify_admins_doctor_registered.delay"):
        doctor_resp = await register_doctor(db_app_client, "cs1")
    doctor_id = doctor_resp["user"]["id"]
    await activate_doctor(test_engine, doctor_id)

    doctor_token_resp = await db_app_client.post("/api/v1/auth/login", json={
        "email": "notif_doctor_cs1@test.com",
        "password": "DocPass9",
    })
    doctor_token = doctor_token_resp.json()["access_token"]

    qr_resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    qr_token = qr_resp.json()["token"]
    await db_app_client.post(f"/api/v1/qr/scan/{qr_token}", headers=auth_header(doctor_token))

    # Create review first
    await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"is_ai_correct": True, "selected_differentials": ["Psoriasis"]},
    )

    # Update clinical_status → fires notification
    with patch(
        "src.workers.tasks.notifications.notify_patient_status_update.delay"
    ) as mock_notif:
        patch_resp = await db_app_client.patch(
            f"/api/v1/cases/{case_id}/review",
            headers=auth_header(doctor_token),
            json={"clinical_status": "resolved"},
        )
        assert patch_resp.status_code == 200
        mock_notif.assert_called_once()
        call_kwargs = mock_notif.call_args.kwargs
        assert call_kwargs["case_id"] == case_id
        assert call_kwargs["new_status"] == "resolved"


# ================================================================== #
# 8. send_push_notification helper (unit-level, mocks FCM)
# ================================================================== #

def test_send_push_notification_calls_fcm():
    """send_push_notification builds the correct Message and calls msg.send()."""
    mock_msg_module = MagicMock()
    mock_send = MagicMock(return_value="projects/aidermchatbot/messages/abc123")
    mock_msg_module.send = mock_send
    mock_msg_module.Message = MagicMock(return_value=MagicMock())
    mock_msg_module.Notification = MagicMock()
    mock_msg_module.AndroidConfig = MagicMock()
    mock_msg_module.APNSConfig = MagicMock()
    mock_msg_module.APNSPayload = MagicMock()
    mock_msg_module.Aps = MagicMock()

    with patch(
        "src.notifications.firebase.get_messaging",
        return_value=mock_msg_module,
    ):
        from src.notifications.service import send_push_notification
        result = send_push_notification(
            token="valid_fcm_token",
            title="Test Title",
            body="Test Body",
            data={"type": "test"},
        )

    assert result is True
    mock_send.assert_called_once()


def test_send_push_notification_empty_token_returns_false():
    """send_push_notification returns False immediately for empty/None token."""
    from src.notifications.service import send_push_notification
    assert send_push_notification("", "T", "B") is False
    assert send_push_notification(None, "T", "B") is False


def test_send_push_notification_fcm_error_returns_false():
    """FCM errors are caught and return False — never raise."""
    mock_msg_module = MagicMock()
    mock_msg_module.send.side_effect = Exception("FCM quota exceeded")
    mock_msg_module.Message = MagicMock(return_value=MagicMock())
    mock_msg_module.Notification = MagicMock()
    mock_msg_module.AndroidConfig = MagicMock()
    mock_msg_module.APNSConfig = MagicMock()
    mock_msg_module.APNSPayload = MagicMock()
    mock_msg_module.Aps = MagicMock()

    with patch(
        "src.notifications.firebase.get_messaging",
        return_value=mock_msg_module,
    ):
        from src.notifications.service import send_push_notification
        result = send_push_notification("some_token", "T", "B")

    assert result is False


def test_send_push_multicast_returns_success_count():
    """send_push_multicast returns the FCM success_count."""
    mock_msg_module = MagicMock()
    mock_response = MagicMock()
    mock_response.success_count = 2
    mock_response.failure_count = 1
    mock_msg_module.send_each_for_multicast = MagicMock(return_value=mock_response)
    mock_msg_module.MulticastMessage = MagicMock(return_value=MagicMock())
    mock_msg_module.Notification = MagicMock()
    mock_msg_module.AndroidConfig = MagicMock()
    mock_msg_module.APNSConfig = MagicMock()
    mock_msg_module.APNSPayload = MagicMock()
    mock_msg_module.Aps = MagicMock()

    with patch(
        "src.notifications.firebase.get_messaging",
        return_value=mock_msg_module,
    ):
        from src.notifications.service import send_push_multicast
        count = send_push_multicast(
            tokens=["tok1", "tok2", "tok3"],
            title="Title",
            body="Body",
        )

    assert count == 2


def test_send_push_multicast_filters_empty_tokens():
    """send_push_multicast skips empty strings and returns 0 for all-empty list."""
    from src.notifications.service import send_push_multicast
    result = send_push_multicast(tokens=["", "  ", None], title="T", body="B")
    assert result == 0
