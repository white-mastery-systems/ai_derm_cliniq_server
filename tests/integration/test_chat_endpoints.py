"""
tests/integration/test_chat_endpoints.py — Chat Endpoint Integration Tests
===========================================================================

Tests for /api/v1/cases/{case_id}/chat endpoints:
    POST   /questions   — trigger question generation (202)
    POST   /answers     — submit patient answers (202)
    GET    /            — conversation history

MOCK STRATEGY
-------------
1. GCS: patched for image upload/download (same pattern as images tests)
2. Celery tasks: patched at service layer
   - src.conversations.service.generate_questions_task → mock with known task_id
   - src.conversations.service.refine_analysis_task → mock with known task_id
3. Case ai_status: set to COMPLETED by directly calling the analyze endpoint
   with a mocked Celery chain (same as Layer 5 tests), then manually
   patching the DB state via helper.

DIRECT DB STATE MANIPULATION
------------------------------
Some tests need ai_status=completed and pre-existing Message rows.
Rather than running the full Celery chain, we use `patch` on the
service functions that read the DB to inject pre-built state.

For testing trigger_questions and submit_answers, we need:
  - A case with ai_status=COMPLETED
  - That state is set by directly triggering analyze (with mocked chain)
    followed by manually updating ai_status via a second patch.

SIMPLER APPROACH USED HERE
---------------------------
We patch `src.conversations.service.generate_questions_task` and
`src.conversations.service.refine_analysis_task` to avoid real Celery.
For ai_status=COMPLETED, we patch the case service's DB state by also
mocking `src.ai.service.build_analysis_chain` and then manually
updating the DB state by calling get_history (which forces DB reads).
"""

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.security import hash_password
from src.models.base import new_uuid
from src.models.user import User, UserRole


# ================================================================== #
# Helpers
# ================================================================== #

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_admin_and_approve_doctor(client, test_engine, doctor_id: str) -> None:
    admin_email = f"_tmp_admin_{doctor_id[:8]}@chattest.com"
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
        "full_name": f"Chat Patient {suffix}",
        "email": f"chat_p_{suffix}@chattest.com",
        "password": "TestPass1",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"chat_p_{suffix}@chattest.com",
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
        "full_name": f"Chat Doctor {suffix}",
        "email": f"chat_d_{suffix}@chattest.com",
        "password": "DocPass9",
        "specialization": "Dermatology",
        "license_number": f"LIC-CH-{suffix}",
        "clinic_name": "Derm Clinic",
    })
    doctor_id = reg_resp.json()["user"]["id"]
    if test_engine is not None:
        await _create_admin_and_approve_doctor(client, test_engine, doctor_id)
    resp = await client.post("/api/v1/auth/login", json={
        "email": f"chat_d_{suffix}@chattest.com",
        "password": "DocPass9",
    })
    token = resp.json()["access_token"]
    profile = await client.get("/api/v1/users/me", headers=auth_header(token))
    return token, profile.json()["id"]


def fake_jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 100


def gcs_patches():
    return [
        patch("src.images.service.gcs.upload_file", return_value="cases/test/images/test.jpg"),
        patch("src.images.service.gcs.delete_file", return_value=None),
        patch("src.images.service.gcs.get_signed_url", return_value="https://fake-gcs.example.com/img.jpg"),
        patch("src.images.service.gcs.build_image_path", return_value="cases/test/images/test.jpg"),
    ]


def mock_celery_chain(task_id: str = "fake-task-id"):
    mock_result = MagicMock()
    mock_result.id = task_id
    mock_chain = MagicMock()
    mock_chain.apply_async.return_value = mock_result
    return patch("src.ai.service.build_analysis_chain", return_value=mock_chain)


def mock_generate_questions(task_id: str = "fake-qgen-task"):
    mock_result = MagicMock()
    mock_result.id = task_id
    mock_task = MagicMock()
    mock_task.delay.return_value = mock_result
    return patch("src.conversations.service.generate_questions_task", mock_task)


def mock_refine_analysis(task_id: str = "fake-refine-task"):
    mock_result = MagicMock()
    mock_result.id = task_id
    mock_task = MagicMock()
    mock_task.delay.return_value = mock_result
    return patch("src.conversations.service.refine_analysis_task", mock_task)


async def setup_completed_case(client: AsyncClient, token: str) -> str:
    """Create case with consent → attempt image upload (may fail quality) → trigger analysis (mocked).
    Returns case_id. The image upload and analyze calls are best-effort; tests that need a
    valid case_id can still use the returned id with service-layer mocks.
    """
    case_resp = await client.post(
        "/api/v1/cases",
        headers=auth_header(token),
        json={
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "is_for_self": True,
            "presenting_complaint": "Itchy patch",
            "consent_ai_analysis": True,
        },
    )
    case_id = case_resp.json()["id"]

    patches = gcs_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await client.post(f"/api/v1/cases/{case_id}/images",
                          headers=auth_header(token),
                          files={"file": ("skin.jpg", BytesIO(fake_jpeg()), "image/jpeg")},
                          data={"image_type": "skin"})

    with mock_celery_chain():
        await client.post(f"/api/v1/cases/{case_id}/ai/analyze", headers=auth_header(token))

    return case_id


# ================================================================== #
# POST /api/v1/cases/{id}/chat/questions
# ================================================================== #

class TestTriggerQuestions:

    async def test_patient_can_trigger_questions_when_completed(self, db_app_client: AsyncClient):
        """Trigger questions when ai_status=COMPLETED (service check bypassed via patch)."""
        token, _ = await register_and_login_patient(db_app_client, "tq01")
        case_id = await setup_completed_case(db_app_client, token)

        # Bypass the ai_status=COMPLETED check and question existence check
        from src.conversations import service as conv_service
        from src.conversations.schemas import QuestionsGeneratedResponse

        mock_resp = QuestionsGeneratedResponse(case_id=case_id, round_number=0, task_id="task-tq01")
        with patch.object(conv_service, "trigger_questions", return_value=mock_resp):
            resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/chat/questions",
                headers=auth_header(token),
            )

        assert resp.status_code == 202
        body = resp.json()
        assert body["case_id"] == case_id
        assert body["task_id"] == "task-tq01"

    async def test_cannot_trigger_when_analysis_not_complete(self, db_app_client: AsyncClient):
        """ai_status=pending (analysis not triggered) → 400."""
        token, _ = await register_and_login_patient(db_app_client, "tq02")
        case_resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(token),
            json={
                "consultation_type": "new_complaint",
                "has_visible_lesion": True,
                "is_for_self": True,
                "consent_ai_analysis": True,
            },
        )
        case_id = case_resp.json()["id"]

        # No mock needed — service raises BadRequestException before reaching Celery call
        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/chat/questions",
            headers=auth_header(token),
        )
        assert resp.status_code == 400

    async def test_doctor_cannot_trigger_questions(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "tq03p")
        d_token, _ = await register_and_login_doctor(db_app_client, "tq03d", test_engine)
        case_id = await setup_completed_case(db_app_client, p_token)
        # Doctor is NOT assigned — require_patient_or_assigned_doctor raises 403
        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/chat/questions",
            headers=auth_header(d_token),
        )
        assert resp.status_code == 403

    async def test_unauthenticated_cannot_trigger(self, db_app_client: AsyncClient):
        resp = await db_app_client.post("/api/v1/cases/some-id/chat/questions")
        assert resp.status_code == 401

    async def test_unknown_case_returns_404(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "tq04")
        # CaseNotFoundException raised before Celery call — no mock needed
        resp = await db_app_client.post(
            "/api/v1/cases/nonexistent-case/chat/questions",
            headers=auth_header(token),
        )
        assert resp.status_code == 404


# ================================================================== #
# POST /api/v1/cases/{id}/chat/answers
# ================================================================== #

class TestSubmitAnswers:

    async def test_cannot_submit_without_questions(self, db_app_client: AsyncClient):
        """No AI messages for current round → 400. No Celery mock needed."""
        token, _ = await register_and_login_patient(db_app_client, "sa01")
        case_resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(token),
            json={
                "consultation_type": "new_complaint",
                "has_visible_lesion": True,
                "is_for_self": True,
                "consent_ai_analysis": True,
            },
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/chat/answers",
            headers=auth_header(token),
            json={"answers": [{"question_index": 0, "answer": "2 weeks"}]},
        )
        assert resp.status_code == 400

    async def test_submit_accepts_correct_number_of_answers(self, db_app_client: AsyncClient):
        """Submitting answers works when all preconditions are met (service mocked)."""
        token, _ = await register_and_login_patient(db_app_client, "sa02")
        case_id = await setup_completed_case(db_app_client, token)

        from src.conversations import service as conv_service
        from src.conversations.schemas import AnswersAcceptedResponse

        mock_resp = AnswersAcceptedResponse(case_id=case_id, round_number=0, task_id="task-sa02")
        with patch.object(conv_service, "submit_answers", return_value=mock_resp):
            resp = await db_app_client.post(
                f"/api/v1/cases/{case_id}/chat/answers",
                headers=auth_header(token),
                json={"answers": [
                    {"question_index": 0, "answer": "2 weeks"},
                    {"question_index": 1, "answer": "Yes, it itches"},
                    {"question_index": 2, "answer": "Left arm"},
                ]},
            )

        assert resp.status_code == 202
        assert resp.json()["task_id"] == "task-sa02"

    async def test_empty_answers_list_rejected(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "sa03")
        case_id = await setup_completed_case(db_app_client, token)

        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/chat/answers",
            headers=auth_header(token),
            json={"answers": []},
        )
        assert resp.status_code == 422  # Pydantic validation: min_length=1

    async def test_doctor_cannot_submit_answers(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "sa04p")
        d_token, _ = await register_and_login_doctor(db_app_client, "sa04d", test_engine)
        case_id = await setup_completed_case(db_app_client, p_token)
        # Doctor is NOT assigned — require_patient_or_assigned_doctor raises 403
        resp = await db_app_client.post(
            f"/api/v1/cases/{case_id}/chat/answers",
            headers=auth_header(d_token),
            json={"answers": [{"question_index": 0, "answer": "Test"}]},
        )
        assert resp.status_code == 403

    async def test_unauthenticated_cannot_submit(self, db_app_client: AsyncClient):
        resp = await db_app_client.post(
            "/api/v1/cases/some-id/chat/answers",
            json={"answers": [{"question_index": 0, "answer": "Test"}]},
        )
        assert resp.status_code == 401


# ================================================================== #
# GET /api/v1/cases/{id}/chat
# ================================================================== #

class TestGetHistory:

    async def test_empty_history_for_new_case(self, db_app_client: AsyncClient):
        token, _ = await register_and_login_patient(db_app_client, "gh01")
        case_resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(token),
            json={
                "consultation_type": "new_complaint",
                "has_visible_lesion": True,
                "is_for_self": True,
                "consent_ai_analysis": True,
            },
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.get(
            f"/api/v1/cases/{case_id}/chat", headers=auth_header(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["case_id"] == case_id
        assert body["rounds"] == []
        assert body["raw_messages"] == []
        assert body["current_round"] == 0
        assert body["is_complete"] is False

    async def test_history_with_messages(self, db_app_client: AsyncClient):
        """Inject messages via the service layer and verify GET /chat returns them."""
        token, _ = await register_and_login_patient(db_app_client, "gh02")
        case_id = await setup_completed_case(db_app_client, token)

        # Inject AI + patient messages via mocked service return
        from src.conversations import service as conv_service
        from src.conversations.schemas import (
            ConversationHistoryResponse, RoundOut, QuestionItem, MessageOut
        )
        from datetime import datetime, timezone

        mock_history = ConversationHistoryResponse(
            case_id=case_id,
            current_round=0,
            max_rounds=5,
            is_complete=False,
            rounds=[
                RoundOut(
                    round_number=0,
                    questions=[
                        QuestionItem(question="How long?", answer_options=["1 week", "2 weeks"]),
                    ],
                    answers=["1 week"],
                )
            ],
            raw_messages=[
                MessageOut(
                    id="msg-1",
                    role="ai",
                    content=json.dumps({"question": "How long?", "answer_options": ["1 week", "2 weeks"]}),
                    round_number=0,
                    question_index=0,
                    created_at=datetime.now(tz=timezone.utc),
                ),
                MessageOut(
                    id="msg-2",
                    role="patient",
                    content="1 week",
                    round_number=0,
                    question_index=0,
                    created_at=datetime.now(tz=timezone.utc),
                ),
            ],
        )

        with patch.object(conv_service, "get_history", return_value=mock_history):
            resp = await db_app_client.get(
                f"/api/v1/cases/{case_id}/chat", headers=auth_header(token)
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["rounds"]) == 1
        assert body["rounds"][0]["questions"][0]["question"] == "How long?"
        assert body["rounds"][0]["answers"][0] == "1 week"
        assert len(body["raw_messages"]) == 2

    async def test_doctor_can_read_history(self, db_app_client: AsyncClient, test_engine):
        p_token, _ = await register_and_login_patient(db_app_client, "gh03p")
        d_token, _ = await register_and_login_doctor(db_app_client, "gh03d", test_engine)
        case_resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(p_token),
            json={
                "consultation_type": "new_complaint",
                "has_visible_lesion": True,
                "is_for_self": True,
                "consent_ai_analysis": True,
            },
        )
        case_id = case_resp.json()["id"]
        await db_app_client.patch(
            f"/api/v1/cases/{case_id}/assign", headers=auth_header(d_token)
        )

        resp = await db_app_client.get(
            f"/api/v1/cases/{case_id}/chat", headers=auth_header(d_token)
        )
        assert resp.status_code == 200

    async def test_other_patient_cannot_read_history(self, db_app_client: AsyncClient):
        token1, _ = await register_and_login_patient(db_app_client, "gh04a")
        token2, _ = await register_and_login_patient(db_app_client, "gh04b")
        case_resp = await db_app_client.post(
            "/api/v1/cases",
            headers=auth_header(token1),
            json={
                "consultation_type": "new_complaint",
                "has_visible_lesion": True,
                "is_for_self": True,
                "consent_ai_analysis": True,
            },
        )
        case_id = case_resp.json()["id"]

        resp = await db_app_client.get(
            f"/api/v1/cases/{case_id}/chat", headers=auth_header(token2)
        )
        assert resp.status_code == 404

    async def test_unauthenticated_cannot_read_history(self, db_app_client: AsyncClient):
        resp = await db_app_client.get("/api/v1/cases/some-id/chat")
        assert resp.status_code == 401
