"""
doctor_review/controller.py — Doctor Review HTTP Endpoints
===========================================================

All routes nested under /api/v1/cases/{case_id} (set in src/api.py).

REVIEW CRUD
-----------
POST   /review          → Doctor creates a review (201)
PATCH  /review          → Doctor updates/completes review (200)
GET    /review          → Patient or doctor reads the review (200)

AI-ASSIST (doctor only — stateless read + Gemini call)
------------------------------------------------------
GET    /review/ai/complaints          → Technical complaint terms (checkboxes)
GET    /review/ai/diagnosis           → Fresh AI differential
GET    /review/ai/questions           → First clarifying question
POST   /review/ai/questions/next      → Next Q&A round (loop)
GET    /review/ai/summary             → Final clinical summary draft
GET    /review/ai/treatment-plan      → Treatment plan + prescription

VOICE NOTE
----------
POST   /review/voice-note             → Whisper audio → transcript

ROLE RULES
----------
POST/PATCH review  : DOCTOR only (must be assigned to the case)
GET    review      : PATIENT (owns case) or DOCTOR (assigned) or ADMIN
All /ai/* routes   : DOCTOR only (must be assigned to the case)
/voice-note        : DOCTOR only
"""

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_doctor
from src.database.core import get_async_session
from src.doctor_review import service
from src.doctor_review import ai_service
from src.doctor_review.schemas import (
    AiComplaintsResponse,
    AiDiagnosisResponse,
    AiQuestionResponse,
    AiSummaryResponse,
    AiTreatmentPlanResponse,
    CreateReviewRequest,
    DoctorReviewResponse,
    NextQuestionRequest,
    UpdateReviewRequest,
    VoiceNoteResponse,
)
from src.models.user import User

router = APIRouter()


# ================================================================== #
# Review CRUD
# ================================================================== #

@router.post(
    "/review",
    response_model=DoctorReviewResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a doctor review for a case",
    description=(
        "Doctor creates their review record for an assigned case. "
        "Must be assigned via QR scan. Returns 409 if a review already exists — use PATCH to update."
    ),
)
async def create_review(
    case_id: str,
    request: CreateReviewRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> DoctorReviewResponse:
    return await service.create_review(db, doctor, case_id, request)


@router.patch(
    "/review",
    response_model=DoctorReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a doctor review",
    description=(
        "Partial update — only provided fields change. "
        "Set `review_status=completed` to finalise the review and unlock report generation. "
        "Set `clinical_status` to update the case badge seen by the patient."
    ),
)
async def update_review(
    case_id: str,
    request: UpdateReviewRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> DoctorReviewResponse:
    return await service.update_review(db, doctor, case_id, request)


@router.get(
    "/review",
    response_model=DoctorReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Get the doctor review for a case",
    description=(
        "Accessible to the patient who owns the case, the assigned doctor, and admins. "
        "Returns 404 if no review has been submitted yet."
    ),
)
async def get_review(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> DoctorReviewResponse:
    return await service.get_review(db, user, case_id)


# ================================================================== #
# AI-Assist
# ================================================================== #

@router.get(
    "/review/ai/complaints",
    response_model=AiComplaintsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get AI-generated technical complaint terms",
    description=(
        "Translates the patient's own words from the Q&A conversation into "
        "precise dermatological clinical terms. Returns a list shown as checkboxes "
        "for the doctor to confirm which findings apply."
    ),
)
async def get_ai_complaints(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> AiComplaintsResponse:
    return await ai_service.get_technical_complaints(db, doctor, case_id)


@router.get(
    "/review/ai/diagnosis",
    response_model=AiDiagnosisResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate a new AI differential for the doctor",
    description=(
        "Runs a fresh differential incorporating all case context — visual description, "
        "patient Q&A, and prescription history. Used for the 'Generate New Differential' button."
    ),
)
async def get_ai_diagnosis(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> AiDiagnosisResponse:
    return await ai_service.generate_new_differential(db, doctor, case_id)


@router.get(
    "/review/ai/questions",
    response_model=AiQuestionResponse,
    status_code=status.HTTP_200_OK,
    summary="Get the first AI clarifying question for the doctor",
    description=(
        "Starts the doctor Q&A loop. Returns the single most important clarifying question "
        "the AI needs answered to refine the diagnosis. "
        "If `has_more=False`, no questions are needed."
    ),
)
async def get_first_question(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> AiQuestionResponse:
    return await ai_service.get_first_question(db, doctor, case_id)


@router.post(
    "/review/ai/questions/next",
    response_model=AiQuestionResponse,
    status_code=status.HTTP_200_OK,
    summary="Get the next AI clarifying question",
    description=(
        "Doctor submits their answer to the previous question and receives the next one. "
        "Send the full `qa_history` array (all rounds so far) and the remaining `questions_left` count. "
        "Loop ends when `has_more=False`."
    ),
)
async def get_next_question(
    case_id: str,
    body: NextQuestionRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> AiQuestionResponse:
    return await ai_service.get_next_question(db, doctor, case_id, body)


@router.get(
    "/review/ai/summary",
    response_model=AiSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate AI final clinical summary",
    description=(
        "Produces a structured clinical summary ready for the medical record. "
        "Uses the doctor's confirmed diagnosis if already set, otherwise uses AI's most probable diagnosis."
    ),
)
async def get_ai_summary(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> AiSummaryResponse:
    return await ai_service.generate_summary(db, doctor, case_id)


@router.get(
    "/review/ai/treatment-plan",
    response_model=AiTreatmentPlanResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate AI treatment plan",
    description=(
        "Returns a structured treatment plan with medications, lifestyle modifications, "
        "dietary recommendations, and a formal prescription list for doctor sign-off."
    ),
)
async def get_ai_treatment_plan(
    case_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> AiTreatmentPlanResponse:
    return await ai_service.generate_treatment_plan(db, doctor, case_id)


# ================================================================== #
# Voice Note
# ================================================================== #

@router.post(
    "/review/voice-note",
    response_model=VoiceNoteResponse,
    status_code=status.HTTP_200_OK,
    summary="Transcribe a voice note using Whisper",
    description=(
        "Upload an audio recording (m4a, mp3, wav, webm). "
        "The file is sent to OpenAI Whisper and the transcript is returned. "
        "Flutter can use the transcript to pre-fill the Ask AI input or save it as a review note."
    ),
)
async def transcribe_voice_note(
    case_id: str,
    audio: UploadFile = File(..., description="Audio file — m4a, mp3, wav, or webm"),
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> VoiceNoteResponse:
    audio_bytes = await audio.read()
    filename = audio.filename or "recording.m4a"
    return await ai_service.transcribe_voice_note(db, doctor, case_id, audio_bytes, filename)
