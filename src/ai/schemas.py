"""
ai/schemas.py — AI Analysis Request & Response Models
======================================================

THREE ENDPOINTS
---------------
1. POST /analyze      → trigger analysis → AnalysisAcceptedResponse (202)
2. GET  /status       → poll progress    → AnalysisStatusResponse (200)
3. GET  /results      → fetch results    → AnalysisResultsResponse (200)

POLLING PATTERN
---------------
The Flutter app triggers analysis, then polls /status every few seconds
until ai_status == "completed" or "failed". On "completed" it fetches
/results once to display the differential to the patient.

STATUS VALUES
-------------
These mirror Case.ai_status:
  pending    — case created, analysis not yet triggered
  processing — Celery chain is running
  completed  — all tasks succeeded, results are saved
  failed     — a task in the chain failed (reason in error_message)

VISUAL DESCRIPTION FIELDS
--------------------------
These mirror the JSON schema returned by ImageAnalysisPrompts.get_description().
The service extracts these fields from the raw JSON before storing.

DIFFERENTIAL FIELDS
-------------------
These mirror the JSON schema from ImageAnalysisPrompts.generate_first_differential().
"""

from datetime import datetime

from pydantic import BaseModel, Field


# ================================================================== #
# Trigger Analysis
# ================================================================== #

class AnalysisAcceptedResponse(BaseModel):
    """
    Returned by POST /analyze (HTTP 202).
    Tells the client the task ID to poll.
    """
    case_id: str
    task_id: str
    message: str = "Analysis started. Poll /status for progress."


# ================================================================== #
# Status
# ================================================================== #

class AnalysisStatusResponse(BaseModel):
    """
    Returned by GET /status.
    The Flutter app polls this until ai_status is 'completed' or 'failed'.
    On completion, recommended_rounds tells Flutter how many Q&A rounds
    the AI suggests — show this to the patient before they confirm depth.
    """
    case_id: str
    ai_status: str          # pending | processing | completed | failed
    task_id: str | None = None
    error_message: str | None = None   # set on failure
    completed_at: datetime | None = None
    recommended_rounds: int | None = None  # AI-suggested Q&A round count (1–15)


# ================================================================== #
# Results
# ================================================================== #

class DifferentialItem(BaseModel):
    """One entry in the differential_diagnoses list."""
    diagnosis: str
    likelihood: str
    key_supporting_features: str


class MostProbableDiagnosis(BaseModel):
    """The top diagnosis from Gemini's output."""
    diagnosis: str
    likelihood: str
    key_supporting_features: str


class VisualDescriptionOut(BaseModel):
    """Lesion description extracted from the first AI analysis pass."""
    round_number: int
    type_of_lesion: str | None = None
    site: str | None = None
    overall_description: str | None = None
    description_json: str           # Raw JSON string (full structured data)
    created_at: datetime


class DifferentialDiagnosisOut(BaseModel):
    """Differential diagnosis from the first AI analysis pass."""
    round_number: int
    is_final: bool
    most_probable_diagnosis: str | None = None
    confidence: str | None = None
    diagnosis_json: str             # Raw JSON string (full structured data)
    created_at: datetime


class AnalysisResultsResponse(BaseModel):
    """
    Returned by GET /results.
    Contains the latest visual description and differential from Gemini.
    Only available when ai_status == 'completed'.
    """
    case_id: str
    ai_status: str
    visual_description: VisualDescriptionOut | None = None
    differential: DifferentialDiagnosisOut | None = None
    case_summary: str | None = None


# ================================================================== #
# Case AI Query  (Ask AI feature on Case Summary screen)
# ================================================================== #

class CaseQueryRequest(BaseModel):
    """POST /cases/{case_id}/ai/query"""
    question: str = Field(min_length=1, max_length=500)


class CaseQueryResponse(BaseModel):
    """Response from the Ask AI endpoint."""
    case_id: str
    question: str
    answer: str


# ================================================================== #
# AI Chat Session  (session-managed Ask AI)
# ================================================================== #

class ChatMessageOut(BaseModel):
    """One message turn in the chat history."""
    id: str
    role: str          # "user" | "assistant"
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AiChatRequest(BaseModel):
    """
    POST /cases/{case_id}/ai/chat

    Send a message. Omit session_id to start a new session.
    Include session_id to continue an existing one.
    """
    message: str = Field(min_length=1, max_length=1000)
    session_id: str | None = None


class AiChatResponse(BaseModel):
    """
    Returned after each message.
    Flutter stores session_id and sends it with every follow-up.
    """
    session_id: str
    answer: str


class AiChatHistoryResponse(BaseModel):
    """GET /cases/{case_id}/ai/chat/{session_id}"""
    session_id: str
    case_id: str
    messages: list[ChatMessageOut]
