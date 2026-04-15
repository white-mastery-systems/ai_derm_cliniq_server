"""
doctor_review/schemas.py — Doctor Review Request & Response Models
==================================================================

ENDPOINTS
---------
POST  /api/v1/cases/{case_id}/review                      → Doctor creates review
PATCH /api/v1/cases/{case_id}/review                      → Doctor updates review
GET   /api/v1/cases/{case_id}/review                      → Read review

AI-ASSIST (stateless — read case data, call Gemini, return suggestion)
-----------------------------------------------------------------------
GET   /api/v1/cases/{case_id}/review/ai/complaints        → Technical complaint terms
GET   /api/v1/cases/{case_id}/review/ai/diagnosis         → New AI differential
GET   /api/v1/cases/{case_id}/review/ai/questions         → First clarifying question
POST  /api/v1/cases/{case_id}/review/ai/questions/next    → Next question (Q&A loop)
GET   /api/v1/cases/{case_id}/review/ai/summary           → Final clinical summary
GET   /api/v1/cases/{case_id}/review/ai/treatment-plan    → Treatment plan JSON

VOICE NOTE
----------
POST  /api/v1/cases/{case_id}/review/voice-note           → Whisper transcription

REVIEW LIFECYCLE
----------------
1. Doctor scans QR → assigned to case
2. POST /review     → creates DoctorReview row (status: in_progress)
3. Doctor runs AI-assist Q&A rounds (GET questions → POST questions/next loop)
4. Doctor validates AI diagnosis (is_ai_correct, selected_differentials)
5. Doctor types confirmed_diagnosis + confidence_level
6. PATCH /review with review_status=completed → marks case done
7. POST /report → generate PDF
"""

from datetime import datetime

from pydantic import BaseModel, Field

from src.models.case import ClinicalStatus
from src.models.doctor_review import ReviewStatus


# ================================================================== #
# Review Create / Update
# ================================================================== #

class CreateReviewRequest(BaseModel):
    """Body for POST /review — doctor starts their review of the case."""
    is_ai_correct: bool | None = Field(
        default=None,
        description="True = AI diagnosis was correct, False = AI was wrong",
    )
    selected_differentials: list[str] | None = Field(
        default=None,
        description="Diagnosis names the doctor marked as correct (checkboxes)",
    )
    confidence_level: str | None = Field(
        default=None,
        pattern="^(low|high)$",
        description="Doctor's confidence in confirmed diagnosis: low | high",
    )
    confirmed_diagnosis: str | None = None
    review_notes: str | None = None
    treatment_plan_json: str | None = None
    qa_history: list[dict] | None = Field(
        default=None,
        description="Q&A rounds: [{question: str, answer: str}, ...]",
    )
    review_status: ReviewStatus = ReviewStatus.IN_PROGRESS


class UpdateReviewRequest(BaseModel):
    """
    Body for PATCH /review — all fields are optional.

    When review_status is set to COMPLETED:
    - reviewed_at timestamp is recorded
    - clinical_status on the case is updated (if provided)
    """
    is_ai_correct: bool | None = None
    selected_differentials: list[str] | None = None
    confidence_level: str | None = Field(
        default=None,
        pattern="^(low|high)$",
    )
    confirmed_diagnosis: str | None = None
    review_notes: str | None = None
    treatment_plan_json: str | None = None
    qa_history: list[dict] | None = None
    review_status: ReviewStatus | None = None
    clinical_status: ClinicalStatus | None = None  # Updates the Case row, not the review


class DoctorReviewResponse(BaseModel):
    """Returned for GET and after POST/PATCH."""
    id: str
    case_id: str
    doctor_id: str
    is_ai_correct: bool | None
    selected_differentials: list[str] = []
    confidence_level: str | None
    confirmed_diagnosis: str | None
    review_notes: str | None
    treatment_plan_json: str | None
    qa_history: list[dict] = []
    review_status: ReviewStatus
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ================================================================== #
# AI-Assist Schemas
# ================================================================== #

class QaRound(BaseModel):
    """One Q&A exchange in the doctor Q&A loop."""
    question: str
    answer: str


class AiComplaintsResponse(BaseModel):
    """
    GET /review/ai/complaints
    Technical dermatological terms translated from patient language.
    Doctor sees these as checkboxes to confirm clinical findings.
    """
    case_id: str
    complaints: list[str]


class AiDiagnosisResponse(BaseModel):
    """
    GET /review/ai/diagnosis
    Fresh AI-generated differential for the doctor.
    Incorporates all case context including Q&A history.
    """
    case_id: str
    diagnosis_json: str   # raw JSON string from Gemini


class AiQuestionResponse(BaseModel):
    """
    GET  /review/ai/questions        ← first question
    POST /review/ai/questions/next   ← subsequent questions
    """
    case_id: str
    question: str
    answer_options: list[str]
    reason: str
    has_more: bool = True  # False when AI has no more doubts


class NextQuestionRequest(BaseModel):
    """
    POST /review/ai/questions/next

    Doctor submits their answer to the previous question.
    The full prior Q&A history is included so the AI can decide
    whether another question is needed.
    """
    qa_history: list[QaRound] = Field(
        description="All Q&A rounds so far, including the answer to the latest question",
    )
    questions_left: int = Field(
        default=5,
        ge=0,
        description="Remaining question budget — AI uses this to prioritise",
    )


class AiSummaryResponse(BaseModel):
    """GET /review/ai/summary"""
    case_id: str
    summary_json: str   # raw JSON from generate_final_summary()


class AiTreatmentPlanResponse(BaseModel):
    """GET /review/ai/treatment-plan"""
    case_id: str
    treatment_plan_json: str  # raw JSON from generate_treatment_plan()


# ================================================================== #
# Voice Note
# ================================================================== #

class VoiceNoteResponse(BaseModel):
    """
    POST /review/voice-note
    Whisper transcription of the recorded audio.
    Flutter receives the transcript and can pre-fill the Ask AI input
    or save it as a review note.
    """
    case_id: str
    transcript: str
