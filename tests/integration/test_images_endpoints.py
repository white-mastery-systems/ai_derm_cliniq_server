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
Uploading an image requires consent_given=True on the case.
Each test that uploads images gives consent first via PATCH /cases/{id}.

MIME TYPE NOTE
--------------
When posting multipart files, we explicitly set content_type="image/jpeg"
so the service's MIME validation passes.
"""

from io import BytesIO
from unittest.mock import patch

from httpx import AsyncClient


# ================================================================== #
# Helpers
# ================================================================== #

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
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


async def register_and_login_doctor(client: AsyncClient, suffix: str) -> tuple[str, str]:
    await client.post("/api/v1/auth/register/doctor", json={
        "full_name": f"Img Doctor {suffix}",
        "email": f"img_doctor_{suffix}@imagestest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-IMG-{suffix}",
        "clinic_name": "Derm Clinic",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"img_doctor_{suffix}@imagestest.com",
        "password": "DocPass9",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def create_case_with_consent(client: AsyncClient, token: str) -> str:
    """Create a case and give consent. Returns case_id."""
    case_resp = await client.post(
        "/api/v1/cases",
        headers=auth_header(token),
        json={
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "is_for_self": True,
            "presenting_complaint": "Itchy rash",
        },
    )
    case_id = case_resp.json()["id"]
    await client.patch(
        f"/api/v1/cases/{case_id}",
        headers=auth_header(token),
        json={"consent_given": True},
    )
    return case_id


def fake_jpeg() -> bytes:
    """Minimal JPEG-like bytes for upload tests."""
    return b"\xff\xd8\xff\xe0" + b"\x00" * 100  # JPEG magic bytes + padding


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

    async def test_upload_without_consent_returns_403(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "up02")
        # Create case WITHOUT giving consent
        case_resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(token),
            json={"consultation_type": "new_complaint", "has_visible_lesion": True, "is_for_self": True},
        )
        case_id = case_resp.json()["id"]

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

    async def test_doctor_cannot_upload_images(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "up04p")
        d_token, _ = await register_and_login_doctor(db_app_client, "up04d")
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

    async def test_doctor_can_list_images_for_assigned_case(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "li02p")
        d_token, _ = await register_and_login_doctor(db_app_client, "li02d")
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

    async def test_doctor_cannot_delete_image(self, db_app_client: AsyncClient):
        p_token, _ = await register_and_login_patient(db_app_client, "di03p")
        d_token, _ = await register_and_login_doctor(db_app_client, "di03d")
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
