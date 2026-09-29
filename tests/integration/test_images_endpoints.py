"""
tests/integration/test_images_endpoints.py — Images Endpoint Integration Tests
================================================================================

Tests for the /api/v1/cases/{case_id}/images endpoints:
    POST   /api/v1/cases/{id}/images            — upload image
    GET    /api/v1/cases/{id}/images            — list images
    GET    /api/v1/cases/{id}/images/{img_id}   — get single image
    DELETE /api/v1/cases/{id}/images/{img_id}   — delete image

GCS MOCK STRATEGY
-----------------
These tests run without real GCS credentials. We patch the three GCS
functions used by the image service:
  - gcs.upload_file      → no-op (returns gcs_path)
  - gcs.delete_file      → no-op
  - gcs.get_signed_url   → returns a fake https URL

We patch at the `src.images.service` module level (where the names are
imported) so the patch takes effect for the duration of each test.

CONSENT REQUIRED
-----------------
Case creation requires consent_ai_analysis=True (set in the POST body).
test_upload_without_consent_returns_403 creates a case without consent
directly via the DB, bypassing the API gate, to test the image-layer check.

DOCTOR APPROVAL
---------------
Doctors are inactive until an admin approves them. Tests that need a
doctor token use _create_admin_and_approve_doctor to activate the account
before logging in.

MIME TYPE NOTE
--------------
When posting multipart files, we explicitly set content_type="image/jpeg"
so the service's MIME validation passes.
"""

from io import BytesIO
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.security import hash_password
from src.models.base import new_uuid
from src.models.case import Case, ConsultationType
from src.models.user import User, UserRole


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_admin_and_approve_doctor(client, test_engine, doctor_id: str) -> None:
    admin_email = f"_tmp_admin_{doctor_id[:8]}@imagestest.com"
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


async def register_and_login_patient(client: AsyncClient, suffix: str) -> tuple[str, str]:
    await client.post("/api/v1/auth/register/patient", json={
        "full_name": f"Img Patient {suffix}",
        "email": f"img_patient_{suffix}@imagestest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"img_patient_{suffix}@imagestest.com",
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


async def register_and_login_doctor(client: AsyncClient, suffix: str, test_engine=None) -> tuple[str, str]:
    reg_resp = await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Img Doctor {suffix}",
        "email": f"img_doctor_{suffix}@imagestest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-IMG-{suffix}",
        "clinic_name": "Derm Clinic",
    })
    doctor_id = reg_resp.json()["user"]["id"]
    if test_engine is not None:
        await _create_admin_and_approve_doctor(client, test_engine, doctor_id)
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"img_doctor_{suffix}@imagestest.com",
        "password": "DocPass9",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def create_case_with_consent(client: AsyncClient, token: str) -> str:
    """Create a case with consent_ai_analysis=True. Returns case_id."""
    case_resp = await client.post(
        "/api/v1/cases",
        headers=auth_header(token),
        json={
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "is_for_self": True,
            "presenting_complaint": "Itchy rash",
            "consent_ai_analysis": True,
        },
    )
    return case_resp.json()["id"]


def fake_jpeg() -> bytes:
    """
    Generate a real JPEG that passes image quality checks.

    Uses Pillow to create a sharp 400×400 grid image — bright enough,
    sharp enough, and the right size to pass brightness, blur, and
    dimension checks in src/images/quality.py.
    """
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (400, 400), color=(200, 200, 200))
    draw = ImageDraw.Draw(img)
    for x in range(0, 400, 20):
        draw.line([(x, 0), (x, 400)], fill=(20, 20, 20), width=2)
    for y in range(0, 400, 20):
        draw.line([(0, y), (400, y)], fill=(20, 20, 20), width=2)
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def gcs_patches():
    """Context manager that stubs all three GCS calls."""
    return [
        patch("src.images.service.gcs.upload_file", return_value="cases/test/images/test.jpg"),
        patch("src.images.service.gcs.delete_file", return_value=None),
        patch("src.images.service.gcs.get_signed_url", return_value="https://fake-gcs.example.com/image.jpg"),
        patch("src.images.service.gcs.build_image_path", return_value="cases/test/images/test.jpg"),
    ]


# ================================================================== #
# POST /api/v1/cases/{id}/images
# ================================================================== #

class TestUploadImage:

    async def test_patient_can_upload_image(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "up01")
        case_id = await create_case_with_consent(db_app_client, token)

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/images",
                headers=auth_header(token),
                files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                data={"image_type": "skin"},
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["case_id"] == case_id
        assert body["mime_type"] == "image/jpeg"
        assert body["upload_order"] == 0

    async def test_upload_without_consent_returns_403(self, db_app_client: AsyncClient, test_engine):
        """
        Image upload to a case without consent is rejected.

        The API requires consent_ai_analysis=True at case creation, so we
        insert a case directly into the DB with consent_ai_analysis=False to
        isolate the image-layer consent check.
        """
        token, patient_id = await register_and_login_patient(db_app_client, "up02")

        factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            case = Case(
                id=new_uuid(),
                patient_id=patient_id,
                consultation_type=ConsultationType.NEW_COMPLAINT,
                consent_ai_analysis=False,
            )
            session.add(case)
            await session.commit()
            case_id = case.id

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/images",
                headers=auth_header(token),
                files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                data={"image_type": "skin"},
            )

        assert resp.status_code == 403

    async def test_upload_unsupported_mime_type_returns_415(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "up03")
        case_id = await create_case_with_consent(db_app_client, token)

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/images",
                headers=auth_header(token),
                files={"file": ("doc.pdf", BytesIO(b"%PDF-1.4"), "application/pdf")},
                data={"image_type": "skin"},
            )

        assert resp.status_code == 400

    async def test_doctor_cannot_upload_images(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "up04p")
        d_token, _ = await register_and_login_doctor(db_app_client, "up04d", test_engine)
        case_id = await create_case_with_consent(db_app_client, p_token)

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/images",
                headers=auth_header(d_token),
                files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                data={"image_type": "skin"},
            )

        assert resp.status_code == 403

    async def test_upload_order_increments(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "up05")
        case_id = await create_case_with_consent(db_app_client, token)

        upload_orders = []
        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            for _ in range(3):
                resp = await db_app_client.post(
                    f"/api/v1/cases/{case_id}/images",
                    headers=auth_header(token),
                    files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                    data={"image_type": "skin"},
                )
                upload_orders.append(resp.json()["upload_order"])

        assert upload_orders == [0, 1, 2]

    async def test_upload_unknown_case_returns_404(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "up06")

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = await db_app_client.post(
                "/api/v1/cases/nonexistent-case/images",
                headers=auth_header(token),
                files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                data={"image_type": "skin"},
            )

        assert resp.status_code == 404


# ================================================================== #
# GET /api/v1/cases/{id}/images
# ================================================================== #

class TestListImages:

    async def test_patient_can_list_own_images(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "li01")
        case_id = await create_case_with_consent(db_app_client, token)

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            for _ in range(2):
                await db_app_client.post(
                    f"/api/v1/cases/{case_id}/images",
                    headers=auth_header(token),
                    files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                    data={"image_type": "skin"},
                )

        with patches[2]:
            resp = await db_app_client.get(
                f"/api/v1/cases/{case_id}/images", headers=auth_header(token)
            )

        assert resp.status_code == 200
        assert resp.json()["total"] == 2
        assert len(resp.json()["images"]) == 2

    async def test_doctor_can_list_images_for_assigned_case(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "li02p")
        d_token, _ = await register_and_login_doctor(db_app_client, "li02d", test_engine)
        case_id = await create_case_with_consent(db_app_client, p_token)

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            await db_app_client.post(
                f"/api/v1/cases/{case_id}/images",
                headers=auth_header(p_token),
                files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                data={"image_type": "skin"},
            )

        await db_app_client.patch(
            f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token)
        )

        with patches[2]:
            resp = await db_app_client.get(
                f"/api/v1/cases/{case_id}/images", headers=auth_header(d_token)
            )

        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_empty_list_returns_zero(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "li03")
        case_id = await create_case_with_consent(db_app_client, token)

        resp = await db_app_client.get(
            f"/api/v1/cases/{case_id}/images", headers=auth_header(token)
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ================================================================== #
# GET /api/v1/cases/{id}/images/{image_id}
# ================================================================== #

class TestGetSingleImage:

    async def test_patient_can_get_image_by_id(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "gi01")
        case_id = await create_case_with_consent(db_app_client, token)

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            upload_resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/images",
                headers=auth_header(token),
                files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                data={"image_type": "skin"},
            )
        image_id = upload_resp.json()["id"]

        with patches[2]:
            resp = await db_app_client.get(
                f"/api/v1/cases/{case_id}/images/{image_id}",
                headers=auth_header(token),
            )

        assert resp.status_code == 200
        assert resp.json()["id"] == image_id

    async def test_unknown_image_id_returns_404(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "gi02")
        case_id = await create_case_with_consent(db_app_client, token)

        resp = await db_app_client.get(
            f"/api/v1/cases/{case_id}/images/nonexistent-img",
            headers=auth_header(token),
        )
        assert resp.status_code == 404


# ================================================================== #
# DELETE /api/v1/cases/{id}/images/{image_id}
# ================================================================== #

class TestDeleteImage:

    async def test_patient_can_delete_own_image(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "di01")
        case_id = await create_case_with_consent(db_app_client, token)

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            upload_resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/images",
                headers=auth_header(token),
                files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                data={"image_type": "skin"},
            )
        image_id = upload_resp.json()["id"]

        with patches[1]:
            resp = await db_app_client.delete(
                f"/api/v1/cases/{case_id}/images/{image_id}",
                headers=auth_header(token),
            )
        assert resp.status_code == 204

    async def test_after_delete_image_not_in_list(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "di02")
        case_id = await create_case_with_consent(db_app_client, token)

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            upload_resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/images",
                headers=auth_header(token),
                files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                data={"image_type": "skin"},
            )
        image_id = upload_resp.json()["id"]

        with patches[1]:
            await db_app_client.delete(
                f"/api/v1/cases/{case_id}/images/{image_id}",
                headers=auth_header(token),
            )

        resp = await db_app_client.get(
            f"/api/v1/cases/{case_id}/images", headers=auth_header(token)
        )
        assert resp.json()["total"] == 0

    async def test_doctor_cannot_delete_image(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "di03p")
        d_token, _ = await register_and_login_doctor(db_app_client, "di03d", test_engine)
        case_id = await create_case_with_consent(db_app_client, p_token)

        patches = gcs_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            upload_resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/images",
                headers=auth_header(p_token),
                files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                data={"image_type": "skin"},
            )
        image_id = upload_resp.json()["id"]

        resp = await db_app_client.delete(
            f"/api/v1/cases/{case_id}/images/{image_id}",
            headers=auth_header(d_token),
        )
        assert resp.status_code == 403

    async def test_delete_nonexistent_image_returns_404(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "di04")
        case_id = await create_case_with_consent(db_app_client, token)

        resp = await db_app_client.delete(
            f"/api/v1/cases/{case_id}/images/nonexistent-img",
            headers=auth_header(token),
        )
        assert resp.status_code == 404
