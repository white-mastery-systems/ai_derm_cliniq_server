"""
tests/integration/test_qr_endpoints.py — QR Endpoint Integration Tests
=======================================================================

Tests for:
    POST  /api/v1/qr/generate        — patient generates QR token
    POST  /api/v1/qr/scan/{token}    — doctor scans QR token

SETUP STRATEGY
--------------
QR generation requires ai_status=COMPLETED. Since we cannot run a real
Celery chain in tests, we set ai_status directly in the test database
using a raw SQLAlchemy session on test_engine.

The shared StaticPool ensures that data committed via db_app_client
(HTTP requests) and data committed via direct session (ai_status update)
are in the same in-memory SQLite instance.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models.case import AiStatus, Case


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def register_and_login_patient(client: AsyncClient, suffix: str) -> tuple[str, str]:
    await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"QR Patient {suffix}",
        "email": f"qr_patient_{suffix}@qrtest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"qr_patient_{suffix}@qrtest.com",
        "password": "TestPass1",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def register_and_login_doctor(client: AsyncClient, suffix: str) -> tuple[str, str]:
    await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"QR Doctor {suffix}",
        "email": f"qr_doctor_{suffix}@qrtest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-QR-{suffix}",
        "clinic_name": "Derm Clinic",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"qr_doctor_{suffix}@qrtest.com",
        "password": "DocPass9",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def create_case(client: AsyncClient, token: str) -> str:
    resp = await client.post(
        "/api/v1/cases",
        headers=auth_header(token),
        json={
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "is_for_self": True,
            "presenting_complaint": "Red itchy patch",
        },
    )
    return resp.json()["id"]


async def set_ai_completed(test_engine, case_id: str) -> None:
    """Directly update ai_status=COMPLETED in the test DB."""
    factory = async_sessionmaker(
        bind=test_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as session:
        case = await session.get(Case, case_id)
        case.ai_status = AiStatus.COMPLETED
        await session.commit()


async def setup_completed_case(client: AsyncClient, test_engine, suffix: str):
    """Full setup: register patient, create case, mark AI complete."""
    patient_token, patient_id = await register_and_login_patient(client, suffix)
    case_id = await create_case(client, patient_token)
    await set_ai_completed(test_engine, case_id)
    return patient_token, patient_id, case_id


# ================================================================== #
# POST /api/v1/qr/generate
# ================================================================== #

@pytest.mark.asyncio
async def test_generate_qr_success(db_app_client: AsyncClient, test_engine):
    """Patient with a completed case gets a QR token."""
    patient_token, _, case_id = await setup_completed_case(
        db_app_client, test_engine, "gen1"
    )

    resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "token" in body
    assert body["case_id"] == case_id
    assert "expires_at" in body
    assert f"/api/v1/qr/scan/{body['token']}" in body["qr_url"]


@pytest.mark.asyncio
async def test_generate_qr_requires_patient(db_app_client: AsyncClient, test_engine):
    """Doctors cannot generate QR codes."""
    _, _, case_id = await setup_completed_case(db_app_client, test_engine, "gen2")
    doctor_token, _ = await register_and_login_doctor(db_app_client, "gen2")

    resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(doctor_token),
        json={"case_id": case_id},
    )
    # require_doctor dependency returns 403 before service is called
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_generate_qr_case_not_found(db_app_client: AsyncClient, test_engine):
    """Generating QR for a non-existent or unowned case returns 404."""
    patient_token, _, _ = await setup_completed_case(
        db_app_client, test_engine, "gen3"
    )

    resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": "nonexistent-case-id"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_generate_qr_requires_ai_completed(db_app_client: AsyncClient):
    """Cannot generate QR while AI analysis is still pending."""
    patient_token, _ = await register_and_login_patient(db_app_client, "gen4")
    case_id = await create_case(db_app_client, patient_token)
    # ai_status is PENDING by default — do NOT mark it completed

    resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    assert resp.status_code == 400
    assert "AI analysis" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_generate_qr_requires_auth(db_app_client: AsyncClient):
    """Unauthenticated request is rejected."""
    resp = await db_app_client.post(
        "/api/v1/qr/generate",
        json={"case_id": "some-case-id"},
    )
    assert resp.status_code == 401


# ================================================================== #
# POST /api/v1/qr/scan/{token}
# ================================================================== #

@pytest.mark.asyncio
async def test_scan_qr_success(db_app_client: AsyncClient, test_engine):
    """Doctor scans a valid QR token and gets case_id + patient name."""
    patient_token, _, case_id = await setup_completed_case(
        db_app_client, test_engine, "scan1"
    )
    doctor_token, _ = await register_and_login_doctor(db_app_client, "scan1")

    # Generate the QR token
    gen_resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    token = gen_resp.json()["token"]

    # Doctor scans it
    scan_resp = await db_app_client.post(
        f"/api/v1/qr/scan/{token}",
        headers=auth_header(doctor_token),
    )
    assert scan_resp.status_code == 200
    body = scan_resp.json()
    assert body["case_id"] == case_id
    assert "patient_name" in body
    assert "message" in body


@pytest.mark.asyncio
async def test_scan_qr_auto_assigns_doctor(db_app_client: AsyncClient, test_engine):
    """Scanning a QR auto-assigns the doctor to the case."""
    patient_token, _, case_id = await setup_completed_case(
        db_app_client, test_engine, "scan2"
    )
    doctor_token, doctor_id = await register_and_login_doctor(db_app_client, "scan2")

    gen_resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    token = gen_resp.json()["token"]

    await db_app_client.post(
        f"/api/v1/qr/scan/{token}",
        headers=auth_header(doctor_token),
    )

    # Verify case now has doctor assigned
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False)
    async with factory() as session:
        case = await session.get(Case, case_id)
        assert case.doctor_id == doctor_id


@pytest.mark.asyncio
async def test_scan_qr_requires_doctor(db_app_client: AsyncClient, test_engine):
    """Patients cannot scan QR codes."""
    patient_token, _, case_id = await setup_completed_case(
        db_app_client, test_engine, "scan3"
    )

    gen_resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    token = gen_resp.json()["token"]

    resp = await db_app_client.post(
        f"/api/v1/qr/scan/{token}",
        headers=auth_header(patient_token),  # patient token, not doctor
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_scan_qr_invalid_token(db_app_client: AsyncClient):
    """Scanning a token that doesn't exist returns 410 Gone."""
    doctor_token, _ = await register_and_login_doctor(db_app_client, "scan4")

    resp = await db_app_client.post(
        "/api/v1/qr/scan/this-token-does-not-exist",
        headers=auth_header(doctor_token),
    )
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_scan_qr_token_used_once(db_app_client: AsyncClient, test_engine):
    """A QR token cannot be scanned twice — second scan returns 410."""
    patient_token, _, case_id = await setup_completed_case(
        db_app_client, test_engine, "scan5"
    )
    doctor_token, _ = await register_and_login_doctor(db_app_client, "scan5")

    gen_resp = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    token = gen_resp.json()["token"]

    # First scan — succeeds
    r1 = await db_app_client.post(
        f"/api/v1/qr/scan/{token}",
        headers=auth_header(doctor_token),
    )
    assert r1.status_code == 200

    # Second scan — rejected
    r2 = await db_app_client.post(
        f"/api/v1/qr/scan/{token}",
        headers=auth_header(doctor_token),
    )
    assert r2.status_code == 410
    assert "already been used" in r2.json()["error"]["message"]


@pytest.mark.asyncio
async def test_scan_qr_requires_auth(db_app_client: AsyncClient):
    """Unauthenticated scan is rejected."""
    resp = await db_app_client.post("/api/v1/qr/scan/sometoken")
    assert resp.status_code == 401
