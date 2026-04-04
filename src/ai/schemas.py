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

from pydantic import BaseModel


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
    """
    case_id: str
    ai_status: str          # pending | processing | completed | failed
    task_id: str | None = None
    error_message: str | None = None   # set on failure
    completed_at: datetime | None = None


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
