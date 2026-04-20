"""
tests/integration/test_doctor_review_endpoints.py — Doctor Review Endpoint Tests
==================================================================================

Tests for:
    POST   /api/v1/cases/{case_id}/review   — create review (doctor only)
    PATCH  /api/v1/cases/{case_id}/review   — update review (doctor only)
    GET    /api/v1/cases/{case_id}/review   — read review (patient or doctor)

SETUP STRATEGY
--------------
Doctor review requires the doctor to be assigned to the case.
Assignment happens via QR scan — so each test that needs a doctor-assigned
case goes through the full generate → scan flow.

ai_status is set directly via test_engine (same approach as QR tests).
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
        "full_name": f"Rev Patient {suffix}",
        "email": f"rev_patient_{suffix}@revtest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"rev_patient_{suffix}@revtest.com",
        "password": "TestPass1",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def register_and_login_doctor(client: AsyncClient, suffix: str) -> tuple[str, str]:
    await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Rev Doctor {suffix}",
        "email": f"rev_doctor_{suffix}@revtest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-REV-{suffix}",
        "clinic_name": "Derm Clinic",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"rev_doctor_{suffix}@revtest.com",
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
            "presenting_complaint": "Skin rash review",
        },
    )
    return resp.json()["id"]


async def set_ai_completed(test_engine, case_id: str) -> None:
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        case = await session.get(Case, case_id)
        case.ai_status = AiStatus.COMPLETED
        await session.commit()


async def setup_with_doctor_assigned(
    client: AsyncClient,
    test_engine,
    suffix: str,
) -> tuple[str, str, str, str, str]:
    """
    Full setup: patient creates case, doctor scans QR → doctor assigned.

    Returns: (patient_token, doctor_token, doctor_id, case_id, qr_token)
    """
    patient_token, _ = await register_and_login_patient(client, suffix)
    doctor_token, doctor_id = await register_and_login_doctor(client, suffix)

    case_id = await create_case(client, patient_token)
    await set_ai_completed(test_engine, case_id)

    # Generate QR
    gen_resp = await client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    qr_token = gen_resp.json()["token"]

    # Doctor scans QR → auto-assigned
    await client.post(
        f"/api/v1/qr/scan/{qr_token}",
        headers=auth_header(doctor_token),
    )

    return patient_token, doctor_token, doctor_id, case_id, qr_token


# ================================================================== #
# POST /api/v1/cases/{case_id}/review
# ================================================================== #

@pytest.mark.asyncio
async def test_create_review_success(db_app_client: AsyncClient, test_engine):
    """Assigned doctor creates a review — returns 201."""
    _, doctor_token, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "cr1"
    )

    resp = await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={
            "confirmed_diagnosis": ["Eczema"],
            "review_notes": "Mild atopic dermatitis on forearm.",
            "review_status": "in_progress",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["case_id"] == case_id
    assert body["confirmed_diagnosis"] == ["Eczema"]
    assert body["review_status"] == "in_progress"
    assert body["reviewed_at"] is None


@pytest.mark.asyncio
async def test_create_review_requires_doctor(db_app_client: AsyncClient, test_engine):
    """Patients cannot create reviews."""
    patient_token, _, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "cr2"
    )

    resp = await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(patient_token),
        json={"review_status": "in_progress"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_review_not_assigned_doctor(db_app_client: AsyncClient, test_engine):
    """A doctor who is NOT assigned to the case cannot create a review."""
    _, _, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "cr3"
    )
    # Register a second doctor — not assigned to the case
    other_doctor_token, _ = await register_and_login_doctor(db_app_client, "cr3_other")

    resp = await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(other_doctor_token),
        json={"review_status": "in_progress"},
    )
    assert resp.status_code == 404  # not 403 — enumeration prevention hides unassigned cases


@pytest.mark.asyncio
async def test_create_review_duplicate_returns_409(db_app_client: AsyncClient, test_engine):
    """Creating a second review for the same case returns 409 Conflict."""
    _, doctor_token, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "cr4"
    )

    # First create — should succeed
    r1 = await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"review_status": "in_progress"},
    )
    assert r1.status_code == 201

    # Second create — should conflict
    r2 = await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"review_status": "in_progress"},
    )
    assert r2.status_code == 409


# ================================================================== #
# PATCH /api/v1/cases/{case_id}/review
# ================================================================== #

@pytest.mark.asyncio
async def test_update_review_success(db_app_client: AsyncClient, test_engine):
    """Doctor can update individual fields via PATCH."""
    _, doctor_token, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "ur1"
    )

    # Create first
    await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"review_status": "in_progress"},
    )

    # Update
    resp = await db_app_client.patch(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={
            "confirmed_diagnosis": ["Psoriasis"],
            "review_notes": "Plaque psoriasis, moderate severity.",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed_diagnosis"] == ["Psoriasis"]
    assert body["review_notes"] == "Plaque psoriasis, moderate severity."


@pytest.mark.asyncio
async def test_update_review_complete_sets_reviewed_at(db_app_client: AsyncClient, test_engine):
    """Setting review_status=completed records reviewed_at timestamp."""
    _, doctor_token, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "ur2"
    )

    await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"confirmed_diagnosis": "Rosacea", "review_status": "in_progress"},
    )

    resp = await db_app_client.patch(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"review_status": "completed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "completed"
    assert body["reviewed_at"] is not None


@pytest.mark.asyncio
async def test_update_review_sets_clinical_status(db_app_client: AsyncClient, test_engine):
    """
    PATCH with clinical_status updates the case's clinical badge,
    not just the review record.
    """
    _, doctor_token, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "ur3"
    )

    await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"review_status": "in_progress"},
    )

    resp = await db_app_client.patch(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={
            "review_status": "completed",
            "clinical_status": "resolved",
        },
    )
    assert resp.status_code == 200

    # Verify case clinical_status was updated
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False)
    async with factory() as session:
        case = await session.get(Case, case_id)
        assert case.clinical_status.value == "resolved"


@pytest.mark.asyncio
async def test_update_review_no_review_returns_404(db_app_client: AsyncClient, test_engine):
    """PATCH before POST returns 404."""
    _, doctor_token, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "ur4"
    )

    resp = await db_app_client.patch(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"confirmed_diagnosis": "Acne"},
    )
    assert resp.status_code == 404


# ================================================================== #
# GET /api/v1/cases/{case_id}/review
# ================================================================== #

@pytest.mark.asyncio
async def test_get_review_by_patient(db_app_client: AsyncClient, test_engine):
    """The patient who owns the case can read the doctor review."""
    patient_token, doctor_token, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "gr1"
    )

    await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"confirmed_diagnosis": ["Vitiligo"], "review_status": "completed"},
    )

    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 200
    assert resp.json()["confirmed_diagnosis"] == ["Vitiligo"]


@pytest.mark.asyncio
async def test_get_review_by_doctor(db_app_client: AsyncClient, test_engine):
    """The assigned doctor can read their own review."""
    _, doctor_token, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "gr2"
    )

    await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"confirmed_diagnosis": ["Melanoma suspected"], "review_status": "in_progress"},
    )

    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
    )
    assert resp.status_code == 200
    assert resp.json()["confirmed_diagnosis"] == ["Melanoma suspected"]


@pytest.mark.asyncio
async def test_get_review_no_review_returns_404(db_app_client: AsyncClient, test_engine):
    """GET before any review is created returns 404."""
    patient_token, _, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "gr3"
    )

    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_review_wrong_patient_returns_404(db_app_client: AsyncClient, test_engine):
    """A different patient cannot read someone else's case review."""
    _, doctor_token, _, case_id, _ = await setup_with_doctor_assigned(
        db_app_client, test_engine, "gr4"
    )

    await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"review_status": "in_progress"},
    )

    # Different patient
    other_token, _ = await register_and_login_patient(db_app_client, "gr4_other")

    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(other_token),
    )
    assert resp.status_code == 404
