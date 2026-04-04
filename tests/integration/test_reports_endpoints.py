"""
tests/integration/test_reports_endpoints.py — Report Endpoint Integration Tests
=================================================================================

Tests for:
    POST /api/v1/cases/{case_id}/report  — trigger report generation (doctor)
    GET  /api/v1/cases/{case_id}/report  — fetch report (patient or doctor)

MOCK STRATEGY
-------------
Two layers of mocking:

1. generate_report_task: patched so no real Celery/Redis/WeasyPrint/GCS is needed.
   The mock returns a MagicMock with a known task_id.

2. gcs.get_signed_url: patched in GET /report tests so no real GCS credentials needed.
   Returns a predictable fake URL.

3. CaseReport row: for GET tests, we insert a CaseReport directly into the test DB
   using test_engine (same pattern as QR/review tests).

SETUP DEPENDENCIES
------------------
Each test that needs a report-ready case goes through:
  patient registers → creates case → AI status set COMPLETED (direct DB)
  → doctor registers → scans QR → auto-assigned
  → doctor POSTs review with status=completed
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models.case import AiStatus, Case
from src.models.case_report import CaseReport, ReportType
from src.models.base import new_uuid


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def register_and_login_patient(client, suffix):
    await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"Rep Patient {suffix}",
        "email": f"rep_patient_{suffix}@reptest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"rep_patient_{suffix}@reptest.com",
        "password": "TestPass1",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def register_and_login_doctor(client, suffix):
    await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Rep Doctor {suffix}",
        "email": f"rep_doctor_{suffix}@reptest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-REP-{suffix}",
        "clinic_name": "Derm Clinic",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"rep_doctor_{suffix}@reptest.com",
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


async def setup_with_completed_review(client, test_engine, suffix):
    """
    Full setup: patient case → AI complete → doctor assigned via QR → review completed.

    Returns: (patient_token, doctor_token, case_id)
    """
    patient_token, _ = await register_and_login_patient(client, suffix)
    doctor_token, _ = await register_and_login_doctor(client, suffix)

    # Patient creates case
    case_resp = await client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "is_for_self": True,
            "presenting_complaint": "Persistent rash",
        },
    )
    case_id = case_resp.json()["id"]

    # AI status → COMPLETED
    await set_ai_completed(test_engine, case_id)

    # Doctor scans QR → auto-assigned
    gen = await client.post(
        "/api/v1/qr/generate",
        headers=auth_header(patient_token),
        json={"case_id": case_id},
    )
    qr_token = gen.json()["token"]
    await client.post(f"/api/v1/qr/scan/{qr_token}", headers=auth_header(doctor_token))

    # Doctor creates + completes review
    await client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"confirmed_diagnosis": "Eczema", "review_status": "in_progress"},
    )
    await client.patch(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"review_status": "completed", "clinical_status": "resolved"},
    )

    return patient_token, doctor_token, case_id


def mock_report_task():
    """Patch generate_report_task so no Celery/PDF/GCS is needed."""
    mock_result = MagicMock()
    mock_result.id = "fake-report-task-id-123"
    mock_task = MagicMock()
    mock_task.delay.return_value = mock_result
    return patch("src.reports.service.generate_report_task", mock_task)


async def insert_report_row(test_engine, case_id: str) -> str:
    """Directly insert a CaseReport into the test DB. Returns report id."""
    report_id = new_uuid()
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        report = CaseReport(
            id=report_id,
            case_id=case_id,
            gcs_path=f"cases/{case_id}/reports/report_doctor.pdf",
            report_type=ReportType.DOCTOR,
            generated_at=datetime.now(tz=timezone.utc),
            download_count=0,
        )
        session.add(report)
        await session.commit()
    return report_id


# ================================================================== #
# POST /api/v1/cases/{case_id}/report
# ================================================================== #

@pytest.mark.asyncio
async def test_trigger_report_success(db_app_client: AsyncClient, test_engine):
    """Doctor triggers report generation after review is completed → 202."""
    _, doctor_token, case_id = await setup_with_completed_review(
        db_app_client, test_engine, "tr1"
    )

    with mock_report_task():
        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/report",
            headers=auth_header(doctor_token),
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["case_id"] == case_id
    assert body["task_id"] == "fake-report-task-id-123"
    assert "message" in body


@pytest.mark.asyncio
async def test_trigger_report_requires_doctor(db_app_client: AsyncClient, test_engine):
    """Patients cannot trigger report generation."""
    patient_token, _, case_id = await setup_with_completed_review(
        db_app_client, test_engine, "tr2"
    )

    resp = await db_app_client.post(
        f"/api/v1/cases/{case_id}/report",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_trigger_report_review_not_completed(db_app_client: AsyncClient, test_engine):
    """Cannot trigger report if review is not yet completed."""
    patient_token, _ = await register_and_login_patient(db_app_client, "tr3a")
    doctor_token, _ = await register_and_login_doctor(db_app_client, "tr3a")

    case_resp = await db_app_client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={"consultation_type": "new_complaint", "has_visible_lesion": True,
              "is_for_self": True, "presenting_complaint": "Test"},
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

    # Create review but do NOT complete it
    await db_app_client.post(
        f"/api/v1/cases/{case_id}/review",
        headers=auth_header(doctor_token),
        json={"review_status": "in_progress"},
    )

    with mock_report_task():
        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/report",
            headers=auth_header(doctor_token),
        )

    assert resp.status_code == 400
    assert "completed" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_trigger_report_not_assigned_doctor(db_app_client: AsyncClient, test_engine):
    """A doctor not assigned to the case gets 403."""
    _, _, case_id = await setup_with_completed_review(
        db_app_client, test_engine, "tr4"
    )
    other_doctor_token, _ = await register_and_login_doctor(db_app_client, "tr4_other")

    with mock_report_task():
        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/report",
            headers=auth_header(other_doctor_token),
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_trigger_report_duplicate_returns_409(db_app_client: AsyncClient, test_engine):
    """Cannot trigger report if one already exists — 409 Conflict."""
    _, doctor_token, case_id = await setup_with_completed_review(
        db_app_client, test_engine, "tr5"
    )

    # Insert report row directly
    await insert_report_row(test_engine, case_id)

    with mock_report_task():
        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/report",
            headers=auth_header(doctor_token),
        )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_trigger_report_requires_auth(db_app_client: AsyncClient):
    resp = await db_app_client.post("/api/v1/cases/some-case/report")
    assert resp.status_code == 401


# ================================================================== #
# GET /api/v1/cases/{case_id}/report
# ================================================================== #

@pytest.mark.asyncio
async def test_get_report_by_doctor(db_app_client: AsyncClient, test_engine):
    """Assigned doctor can fetch the report."""
    _, doctor_token, case_id = await setup_with_completed_review(
        db_app_client, test_engine, "gr1"
    )
    await insert_report_row(test_engine, case_id)

    with patch("src.reports.service.gcs.get_signed_url", return_value="https://fake-gcs.example.com/report.pdf"):
        resp = await db_app_client.get(
            f"/api/v1/cases/{case_id}/report",
            headers=auth_header(doctor_token),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["case_id"] == case_id
    assert body["download_url"] == "https://fake-gcs.example.com/report.pdf"
    assert body["report_type"] == "doctor"
    assert "generated_at" in body


@pytest.mark.asyncio
async def test_get_report_by_patient(db_app_client: AsyncClient, test_engine):
    """Patient who owns the case can fetch the report."""
    patient_token, _, case_id = await setup_with_completed_review(
        db_app_client, test_engine, "gr2"
    )
    await insert_report_row(test_engine, case_id)

    with patch("src.reports.service.gcs.get_signed_url", return_value="https://fake-gcs.example.com/report.pdf"):
        resp = await db_app_client.get(
            f"/api/v1/cases/{case_id}/report",
            headers=auth_header(patient_token),
        )

    assert resp.status_code == 200
    assert resp.json()["case_id"] == case_id


@pytest.mark.asyncio
async def test_get_report_increments_download_count(db_app_client: AsyncClient, test_engine):
    """Each GET increments download_count."""
    _, doctor_token, case_id = await setup_with_completed_review(
        db_app_client, test_engine, "gr3"
    )
    await insert_report_row(test_engine, case_id)

    with patch("src.reports.service.gcs.get_signed_url", return_value="https://fake.example.com/r.pdf"):
        r1 = await db_app_client.get(
            f"/api/v1/cases/{case_id}/report",
            headers=auth_header(doctor_token),
        )
        r2 = await db_app_client.get(
            f"/api/v1/cases/{case_id}/report",
            headers=auth_header(doctor_token),
        )

    assert r1.json()["download_count"] == 1
    assert r2.json()["download_count"] == 2


@pytest.mark.asyncio
async def test_get_report_not_generated_returns_404(db_app_client: AsyncClient, test_engine):
    """GET before report is generated returns 404."""
    patient_token, _, case_id = await setup_with_completed_review(
        db_app_client, test_engine, "gr4"
    )

    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/report",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_report_wrong_patient_returns_404(db_app_client: AsyncClient, test_engine):
    """A different patient cannot access another patient's report."""
    _, _, case_id = await setup_with_completed_review(
        db_app_client, test_engine, "gr5"
    )
    await insert_report_row(test_engine, case_id)

    other_patient_token, _ = await register_and_login_patient(db_app_client, "gr5_other")

    with patch("src.reports.service.gcs.get_signed_url", return_value="https://fake.example.com/r.pdf"):
        resp = await db_app_client.get(
            f"/api/v1/cases/{case_id}/report",
            headers=auth_header(other_patient_token),
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_report_requires_auth(db_app_client: AsyncClient):
    resp = await db_app_client.get("/api/v1/cases/some-case/report")
    assert resp.status_code == 401
