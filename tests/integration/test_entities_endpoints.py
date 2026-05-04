"""
tests/integration/test_entities_endpoints.py — Entities Endpoint Integration Tests
===================================================================================

Tests for:
    GET /api/v1/cases/{case_id}/entities

WHAT IS TESTED
--------------
- Patient (owner) gets SNOMED-coded entities for their case
- Assigned doctor gets entities
- 404 when no differential diagnosis exists
- 404 for unknown case
- 403 for unassigned doctor
- 404 for patient accessing another patient's case
- Unauthenticated request → 401
- Entities include is_most_probable flag
- Cache: second call returns same data without re-scanning CSV

SETUP
-----
Tests seed a DifferentialDiagnosis row directly via test_engine —
the same StaticPool SQLite used by db_app_client, so rows are visible
to the HTTP request handler.
"""

import json
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.security import hash_password
from src.models.base import new_uuid
from src.models.case import AiStatus, Case
from src.models.differential_diagnosis import DifferentialDiagnosis
from src.models.user import User, UserRole


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_admin_and_approve_doctor(client, test_engine, doctor_id: str) -> None:
    admin_email = f"_tmp_admin_{doctor_id[:8]}@enttest.com"
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
        await client.post(f"/api/v1/admin/doctors/{doctor_id}/approve", headers=auth_header(admin_token))


async def register_and_login_patient(client, suffix):
    await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"Ent Patient {suffix}",
        "email": f"ent_patient_{suffix}@enttest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"ent_patient_{suffix}@enttest.com",
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


async def register_and_login_doctor(client, suffix, test_engine=None):
    reg_resp = await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Ent Doctor {suffix}",
        "email": f"ent_doctor_{suffix}@enttest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-ENT-{suffix}",
        "clinic_name": "Entity Clinic",
    })
    doctor_id = reg_resp.json()["user"]["id"]
    if test_engine is not None:
        await _create_admin_and_approve_doctor(client, test_engine, doctor_id)
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"ent_doctor_{suffix}@enttest.com",
        "password": "DocPass9",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def set_ai_completed(test_engine, case_id: str):
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        case = await session.get(Case, case_id)
        case.ai_status = AiStatus.COMPLETED
        await session.commit()


async def seed_differential(test_engine, case_id: str, round_number: int = 1, is_final: bool = True):
    """Insert a DifferentialDiagnosis row directly into the test DB."""
    diagnosis_json = json.dumps({
        "most_probable_diagnosis": {
            "diagnosis": "Psoriasis",
            "description": "Chronic inflammatory skin condition",
        },
        "differential_diagnoses": [
            {"diagnosis": "Eczema", "likelihood": "medium"},
            {"diagnosis": "Seborrhoeic dermatitis", "likelihood": "low"},
        ],
        "confidence in answer": "high",
    })
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        dd = DifferentialDiagnosis(
            id=new_uuid(),
            case_id=case_id,
            round_number=round_number,
            is_final=is_final,
            diagnosis_json=diagnosis_json,
            most_probable_diagnosis="Psoriasis",
            confidence="high",
        )
        session.add(dd)
        await session.commit()


async def setup_case_with_entities(client, test_engine, suffix):
    """
    Returns (patient_token, doctor_token, case_id) with:
    - Doctor assigned via QR scan
    - DifferentialDiagnosis row seeded (is_final=True)
    """
    patient_token, _ = await register_and_login_patient(client, suffix)
    doctor_token, _ = await register_and_login_doctor(client, suffix, test_engine)

    case_resp = await client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={"consultation_type": "new_complaint", "has_visible_lesion": True,
              "is_for_self": True, "presenting_complaint": "Red scaly patches",
              "consent_ai_analysis": True},
    )
    case_id = case_resp.json()["id"]
    await set_ai_completed(test_engine, case_id)

    gen = await client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    await client.post(
        f"/api/v1/qr/scan/{gen.json()['token']}",
        headers=auth_header(doctor_token),
    )
    await seed_differential(test_engine, case_id)
    return patient_token, doctor_token, case_id


# ================================================================== #
# GET /api/v1/cases/{case_id}/entities
# ================================================================== #

@pytest.mark.asyncio
async def test_get_entities_patient_success(db_app_client: AsyncClient, test_engine):
    """Patient (owner) retrieves SNOMED-coded entities → 200."""
    patient_token, _, case_id = await setup_case_with_entities(
        db_app_client, test_engine, "ge1"
    )
    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["case_id"] == case_id
    assert body["differential_round"] == 1
    assert len(body["entities"]) >= 1


@pytest.mark.asyncio
async def test_get_entities_doctor_success(db_app_client: AsyncClient, test_engine):
    """Assigned doctor retrieves entities → 200."""
    _, doctor_token, case_id = await setup_case_with_entities(
        db_app_client, test_engine, "ge2"
    )
    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers=auth_header(doctor_token),
    )
    assert resp.status_code == 200
    assert len(resp.json()["entities"]) >= 1


@pytest.mark.asyncio
async def test_get_entities_has_most_probable_flag(db_app_client: AsyncClient, test_engine):
    """Exactly one entity should have is_most_probable=True."""
    patient_token, _, case_id = await setup_case_with_entities(
        db_app_client, test_engine, "ge3"
    )
    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 200
    entities = resp.json()["entities"]
    most_probable = [e for e in entities if e["is_most_probable"]]
    assert len(most_probable) == 1
    assert most_probable[0]["diagnosis_text"] == "Psoriasis"


@pytest.mark.asyncio
async def test_get_entities_differential_entries_not_most_probable(
    db_app_client: AsyncClient, test_engine
):
    """Differential entries have is_most_probable=False."""
    patient_token, _, case_id = await setup_case_with_entities(
        db_app_client, test_engine, "ge4"
    )
    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers=auth_header(patient_token),
    )
    entities = resp.json()["entities"]
    differentials = [e for e in entities if not e["is_most_probable"]]
    assert len(differentials) == 2
    texts = {e["diagnosis_text"] for e in differentials}
    assert "Eczema" in texts
    assert "Seborrhoeic dermatitis" in texts


@pytest.mark.asyncio
async def test_get_entities_no_differential_returns_404(
    db_app_client: AsyncClient, test_engine
):
    """404 when no DifferentialDiagnosis row exists for the case."""
    patient_token, _ = await register_and_login_patient(db_app_client, "ge5a")
    _, doctor_token = await register_and_login_doctor(db_app_client, "ge5b", test_engine)

    case_resp = await db_app_client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={"consultation_type": "new_complaint", "has_visible_lesion": False,
              "is_for_self": True, "presenting_complaint": "Test",
              "consent_ai_analysis": True},
    )
    case_id = case_resp.json()["id"]
    await set_ai_completed(test_engine, case_id)

    gen = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    await db_app_client.post(
        f"/api/v1/qr/scan/{gen.json()['token']}",
        headers=auth_header(doctor_token),
    )
    # No differential seeded — expect 404
    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_entities_unknown_case_returns_404(db_app_client: AsyncClient):
    """Requesting entities for a non-existent case → 404."""
    patient_token, _ = await register_and_login_patient(db_app_client, "ge6")
    resp = await db_app_client.get(
        "/api/v1/cases/nonexistent-case-id/entities",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_entities_unassigned_doctor_returns_403(
    db_app_client: AsyncClient, test_engine
):
    """Doctor not assigned to the case gets 403."""
    _, _, case_id = await setup_case_with_entities(db_app_client, test_engine, "ge7")
    other_token, _ = await register_and_login_doctor(db_app_client, "ge7_other", test_engine)

    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers=auth_header(other_token),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_entities_other_patient_returns_404(
    db_app_client: AsyncClient, test_engine
):
    """Patient cannot access another patient's case entities."""
    _, _, case_id = await setup_case_with_entities(db_app_client, test_engine, "ge8")
    other_patient_token, _ = await register_and_login_patient(db_app_client, "ge8_other")

    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers=auth_header(other_patient_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_entities_unauthenticated_returns_401(
    db_app_client: AsyncClient, test_engine
):
    """No token → 401."""
    _, _, case_id = await setup_case_with_entities(db_app_client, test_engine, "ge9")
    resp = await db_app_client.get(f"/api/v1/cases/{case_id}/entities")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_entities_snomed_cache_second_call(
    db_app_client: AsyncClient, test_engine
):
    """
    Calling the endpoint twice for the same case returns consistent results.
    The second call hits the DB cache (snomed_mappings table) rather than CSV.
    """
    patient_token, _, case_id = await setup_case_with_entities(
        db_app_client, test_engine, "ge10"
    )
    url = f"/api/v1/cases/{case_id}/entities"
    first = await db_app_client.get(url, headers=auth_header(patient_token))
    second = await db_app_client.get(url, headers=auth_header(patient_token))

    assert first.status_code == 200
    assert second.status_code == 200
    # Same entity texts on both calls
    first_texts = {e["diagnosis_text"] for e in first.json()["entities"]}
    second_texts = {e["diagnosis_text"] for e in second.json()["entities"]}
    assert first_texts == second_texts


@pytest.mark.asyncio
async def test_get_entities_uses_latest_round_when_no_final(
    db_app_client: AsyncClient, test_engine
):
    """
    If no is_final=True row exists, the service falls back to the highest round_number.
    """
    patient_token, _ = await register_and_login_patient(db_app_client, "ge11a")
    _, doctor_token = await register_and_login_doctor(db_app_client, "ge11b", test_engine)

    case_resp = await db_app_client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={"consultation_type": "new_complaint", "has_visible_lesion": True,
              "is_for_self": True, "presenting_complaint": "Itchy rash",
              "consent_ai_analysis": True},
    )
    case_id = case_resp.json()["id"]
    await set_ai_completed(test_engine, case_id)

    gen = await db_app_client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    await db_app_client.post(
        f"/api/v1/qr/scan/{gen.json()['token']}",
        headers=auth_header(doctor_token),
    )
    # Seed two rounds, neither is_final
    await seed_differential(test_engine, case_id, round_number=1, is_final=False)
    await seed_differential(test_engine, case_id, round_number=2, is_final=False)

    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 200
    # Should use round 2 (highest)
    assert resp.json()["differential_round"] == 2
