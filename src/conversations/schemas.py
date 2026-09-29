"""
conversations/schemas.py — Chat Request & Response Models
==========================================================

THREE ENDPOINTS
---------------
POST /questions      → 202 — trigger AI question generation for current round
POST /answers        → 202 — submit patient answers + trigger re-analysis
GET  /              → 200 — full conversation history

QUESTION FORMAT
---------------
Each AI question is stored as a JSON string in Message.content:
    {"question": "How long have you had this?", "answer_options": ["< 1 week", "1–4 weeks", ...]}

This is parsed back into QuestionItem for the API response.

CONVERSATION STATE
------------------
is_complete = True when question_round >= max_question_rounds.
The Flutter app uses this to stop the Q&A flow and show the case summary.
"""

from datetime import datetime

from pydantic import BaseModel, Field


# ================================================================== #
# Questions
# ================================================================== #

class QuestionItem(BaseModel):
    """One AI-generated question with selectable answer options."""
    question: str
    answer_options: list[str]


class QuestionsGeneratedResponse(BaseModel):
    """
    Returned by POST /questions (202).
    task_id lets the client poll for when questions are ready.
    """
    case_id: str
    round_number: int
    task_id: str
    message: str = "Generating questions. Poll GET /chat to see them appear."


# ================================================================== #
# Answers
# ================================================================== #

class AnswerItem(BaseModel):
    """One patient answer for a specific question in the current round."""
    question_index: int = Field(ge=0, description="0-indexed position in the question set")
    answer: str = Field(min_length=1, max_length=2000)
    image_url: str | None = Field(default=None, description="Optional signed URL of an image uploaded with this answer via POST /cases/{id}/images")


class SubmitAnswersRequest(BaseModel):
    """
    POST /answers body.
    The client submits all answers for the current round at once.
    Partial submission (answering fewer questions than were asked) is rejected.
    """
    answers: list[AnswerItem] = Field(min_length=1)


class AnswersAcceptedResponse(BaseModel):
    """Returned by POST /answers (202)."""
    case_id: str
    round_number: int
    task_id: str
    message: str = "Answers saved. Re-analysis is in progress. Poll GET /chat for new questions."


# ================================================================== #
# Conversation History
# ================================================================== #

class MessageOut(BaseModel):
    """One message row returned in the conversation history."""
    id: str
    role: str                       # "ai" | "patient"
    content: str                    # Raw content (JSON for AI questions, plain text for answers)
    round_number: int
    question_index: int | None
    image_url: str | None = None    # Signed URL of image attached to this answer (patient only)
    created_at: datetime

    model_config = {"from_attributes": True}


class RoundOut(BaseModel):
    """All messages for one Q&A round, grouped for display."""
    round_number: int
    questions: list[QuestionItem]   # Parsed from AI message content
    answers: list[str]              # Patient's selected answer texts (ordered by question_index)


class FinishChatResponse(BaseModel):
    """Returned by POST /finish (200)."""
    case_id: str
    question_round: int
    max_rounds: int
    is_complete: bool
    message: str = "Conversation finished early. Case summary is ready."


class ConversationHistoryResponse(BaseModel):
    """
    Returned by GET /chat.
    The Flutter app uses `rounds` to render the conversation.
    `raw_messages` is for debugging / doctor review.
    """
    case_id: str
    current_round: int
    max_rounds: int
    is_complete: bool           # True when current_round >= max_rounds
    rounds: list[RoundOut]      # Grouped, parsed view for Flutter
    raw_messages: list[MessageOut]  # All messages in insertion order
