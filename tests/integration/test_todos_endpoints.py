"""
tests/integration/test_todos_endpoints.py — Todo Endpoint Integration Tests
============================================================================

Tests for:
    POST   /api/v1/cases/{case_id}/todos            — create todo (doctor)
    GET    /api/v1/cases/{case_id}/todos            — list todos (patient or doctor)
    PATCH  /api/v1/cases/{case_id}/todos/{todo_id}  — update todo (doctor)
    DELETE /api/v1/cases/{case_id}/todos/{todo_id}  — delete todo (doctor)

SETUP
-----
Doctor must be assigned to the case. Uses QR scan to assign
(same pattern as Layer 7/8 tests).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from unittest.mock import patch

from src.auth.security import hash_password
from src.models.base import new_uuid
from src.models.case import AiStatus, Case
from src.models.user import User, UserRole


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def register_and_login_patient(client, suffix):
    await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"Todo Patient {suffix}",
        "email": f"todo_patient_{suffix}@todotest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"todo_patient_{suffix}@todotest.com",
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
    admin_email = f"_tmp_admin_{doctor_id[:8]}@todotest.com"
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


async def register_and_login_doctor(client, suffix, test_engine=None):
    reg_resp = await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Todo Doctor {suffix}",
        "email": f"todo_doctor_{suffix}@todotest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-TODO-{suffix}",
        "clinic_name": "Todo Clinic",
    })
    doctor_id = reg_resp.json()["user"]["id"]
    if test_engine is not None:
        await _create_admin_and_approve_doctor(client, test_engine, doctor_id)
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"todo_doctor_{suffix}@todotest.com",
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


async def setup_doctor_assigned_case(client, test_engine, suffix):
    """Returns (patient_token, doctor_token, case_id)."""
    patient_token, _ = await register_and_login_patient(client, suffix)
    doctor_token, _ = await register_and_login_doctor(client, suffix, test_engine)

    case_resp = await client.post(
        "/api/v1/cases",
        headers=auth_header(patient_token),
        json={"consultation_type": "new_complaint", "has_visible_lesion": True,
              "is_for_self": True, "presenting_complaint": "Test",
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
    return patient_token, doctor_token, case_id


# ================================================================== #
# POST /api/v1/cases/{case_id}/todos
# ================================================================== #

@pytest.mark.asyncio
async def test_create_todo_success(db_app_client: AsyncClient, test_engine):
    """Assigned doctor creates a todo → 201."""
    _, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "ct1"
    )
    resp = await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
        json={"title": "Order patch test", "description": "Check for nickel allergy"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Order patch test"
    assert body["is_completed"] is False
    assert body["completed_at"] is None
    assert body["case_id"] == case_id


@pytest.mark.asyncio
async def test_create_todo_requires_doctor(db_app_client: AsyncClient, test_engine):
    """Patients cannot create todos."""
    patient_token, _, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "ct2"
    )
    resp = await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(patient_token),
        json={"title": "Test"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_todo_not_assigned_doctor(db_app_client: AsyncClient, test_engine):
    """A different unassigned doctor cannot create todos."""
    _, _, case_id = await setup_doctor_assigned_case(db_app_client, test_engine, "ct3")
    other_token, _ = await register_and_login_doctor(db_app_client, "ct3_other", test_engine)

    resp = await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(other_token),
        json={"title": "Test"},
    )
    assert resp.status_code == 403


# ================================================================== #
# GET /api/v1/cases/{case_id}/todos
# ================================================================== #

@pytest.mark.asyncio
async def test_list_todos_by_doctor(db_app_client: AsyncClient, test_engine):
    """Assigned doctor can list todos."""
    _, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "lt1"
    )
    # Create two todos
    await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
        json={"title": "Todo 1"},
    )
    await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
        json={"title": "Todo 2"},
    )
    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_list_todos_by_patient(db_app_client: AsyncClient, test_engine):
    """Patient who owns the case can read todos."""
    patient_token, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "lt2"
    )
    await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
        json={"title": "Follow up in 2 weeks"},
    )
    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(patient_token),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_list_todos_empty(db_app_client: AsyncClient, test_engine):
    """Returns empty list when no todos exist."""
    _, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "lt3"
    )
    resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ================================================================== #
# PATCH /api/v1/cases/{case_id}/todos/{todo_id}
# ================================================================== #

@pytest.mark.asyncio
async def test_update_todo_title(db_app_client: AsyncClient, test_engine):
    """Doctor can update todo title."""
    _, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "ut1"
    )
    create = await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
        json={"title": "Original title"},
    )
    todo_id = create.json()["id"]

    resp = await db_app_client.patch(
        f"/api/v1/cases/{case_id}/todos/{todo_id}",
        headers=auth_header(doctor_token),
        json={"title": "Updated title"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated title"


@pytest.mark.asyncio
async def test_complete_todo_sets_completed_at(db_app_client: AsyncClient, test_engine):
    """Marking is_completed=true records completed_at timestamp."""
    _, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "ut2"
    )
    create = await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
        json={"title": "Book follow-up appointment"},
    )
    todo_id = create.json()["id"]

    resp = await db_app_client.patch(
        f"/api/v1/cases/{case_id}/todos/{todo_id}",
        headers=auth_header(doctor_token),
        json={"is_completed": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_completed"] is True
    assert body["completed_at"] is not None


@pytest.mark.asyncio
async def test_uncomplete_todo_clears_completed_at(db_app_client: AsyncClient, test_engine):
    """Marking is_completed=false clears completed_at."""
    _, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "ut3"
    )
    create = await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
        json={"title": "Task"},
    )
    todo_id = create.json()["id"]

    await db_app_client.patch(
        f"/api/v1/cases/{case_id}/todos/{todo_id}",
        headers=auth_header(doctor_token),
        json={"is_completed": True},
    )
    resp = await db_app_client.patch(
        f"/api/v1/cases/{case_id}/todos/{todo_id}",
        headers=auth_header(doctor_token),
        json={"is_completed": False},
    )
    assert resp.status_code == 200
    assert resp.json()["is_completed"] is False
    assert resp.json()["completed_at"] is None


@pytest.mark.asyncio
async def test_update_todo_not_found(db_app_client: AsyncClient, test_engine):
    _, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "ut4"
    )
    resp = await db_app_client.patch(
        f"/api/v1/cases/{case_id}/todos/nonexistent-todo",
        headers=auth_header(doctor_token),
        json={"title": "Updated"},
    )
    assert resp.status_code == 404


# ================================================================== #
# DELETE /api/v1/cases/{case_id}/todos/{todo_id}
# ================================================================== #

@pytest.mark.asyncio
async def test_delete_todo_success(db_app_client: AsyncClient, test_engine):
    """Doctor can delete their own todo → 204."""
    _, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "dt1"
    )
    create = await db_app_client.post(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
        json={"title": "To be deleted"},
    )
    todo_id = create.json()["id"]

    resp = await db_app_client.delete(
        f"/api/v1/cases/{case_id}/todos/{todo_id}",
        headers=auth_header(doctor_token),
    )
    assert resp.status_code == 204

    # Confirm it's gone
    list_resp = await db_app_client.get(
        f"/api/v1/cases/{case_id}/todos",
        headers=auth_header(doctor_token),
    )
    assert list_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_delete_todo_not_found(db_app_client: AsyncClient, test_engine):
    _, doctor_token, case_id = await setup_doctor_assigned_case(
        db_app_client, test_engine, "dt2"
    )
    resp = await db_app_client.delete(
        f"/api/v1/cases/{case_id}/todos/nonexistent-todo",
        headers=auth_header(doctor_token),
    )
    assert resp.status_code == 404
