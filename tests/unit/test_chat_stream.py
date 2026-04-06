"""
tests/unit/test_chat_stream.py — SSE Chat Stream Tests
=======================================================

Tests for src/conversations/stream.py

APPROACH
--------
- All DB calls are mocked — no real session needed
- Test the generator directly by collecting yielded events
- Verify event names and data for each trigger (status change, new messages, etc.)
- Test error paths: access denied, case not found
- Test heartbeat and timeout (with shortened intervals)
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.conversations.stream import _sse, _ping, chat_stream_generator
from src.exceptions import CaseNotFoundException
from src.models.case import AiStatus
from src.models.user import UserRole


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def parse_sse(raw: str) -> tuple[str, dict]:
    """Parse 'event: X\\ndata: {...}\\n\\n' into (event_name, data_dict)."""
    lines = raw.strip().split("\n")
    event = ""
    data = {}
    for line in lines:
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
    return event, data


def _make_user(role: UserRole = UserRole.PATIENT, user_id: str = "patient-1"):
    user = MagicMock()
    user.id = user_id
    user.role = role
    return user


def _make_case(
    case_id: str = "case-1",
    patient_id: str = "patient-1",
    doctor_id: str | None = None,
    ai_status: AiStatus = AiStatus.COMPLETED,
    question_round: int = 0,
    max_question_rounds: int = 3,
    case_summary: str | None = None,
):
    case = MagicMock()
    case.id = case_id
    case.patient_id = patient_id
    case.doctor_id = doctor_id
    case.ai_status = ai_status
    case.question_round = question_round
    case.max_question_rounds = max_question_rounds
    case.case_summary = case_summary
    return case


async def _collect(gen, limit: int = 20) -> list[tuple[str, dict]]:
    """Collect up to `limit` events from the async generator."""
    events = []
    async for chunk in gen:
        if not chunk.strip():
            continue
        events.append(parse_sse(chunk))
        if len(events) >= limit:
            break
    return events


# ------------------------------------------------------------------ #
# _sse and _ping helpers
# ------------------------------------------------------------------ #

def test_sse_format():
    raw = _sse("status_update", {"key": "value"})
    assert raw.startswith("event: status_update\n")
    assert '"key": "value"' in raw
    assert raw.endswith("\n\n")


def test_ping_format():
    raw = _ping()
    assert raw.startswith("event: ping\n")
    data = json.loads(raw.split("data: ")[1].strip())
    assert "t" in data


# ------------------------------------------------------------------ #
# Access denied
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_stream_emits_error_when_case_not_found():
    db = AsyncMock()
    user = _make_user()

    with patch(
        "src.conversations.stream._check_access",
        new=AsyncMock(side_effect=CaseNotFoundException(message="No case found")),
    ):
        gen = chat_stream_generator(db, "bad-case-id", user)
        events = await _collect(gen)

    assert len(events) == 1
    event, data = events[0]
    assert event == "error"
    assert "No case found" in data["message"]


# ------------------------------------------------------------------ #
# Initial state emission
# ------------------------------------------------------------------ #

def _make_db_with_case(case):
    """
    Build a mock db whose execute() returns a result whose
    scalar_one_or_none() returns the given case synchronously.
    """
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=case)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_result)
    return db


@pytest.mark.asyncio
async def test_stream_emits_initial_status_on_connect():
    user = _make_user()
    case = _make_case(ai_status=AiStatus.COMPLETED, question_round=0)
    db = _make_db_with_case(case)

    with patch("src.conversations.stream._check_access", new=AsyncMock(return_value=case)), \
         patch("src.conversations.stream._get_ai_message_ids", new=AsyncMock(return_value=set())), \
         patch("src.conversations.stream.POLL_INTERVAL_SECONDS", 0.01), \
         patch("src.conversations.stream.MAX_STREAM_SECONDS", 0.05):

        gen = chat_stream_generator(db, "case-1", user)
        events = await _collect(gen, limit=5)

    # First event must be status_update
    event, data = events[0]
    assert event == "status_update"
    assert data["ai_status"] == "completed"
    assert data["case_id"] == "case-1"


@pytest.mark.asyncio
async def test_stream_emits_existing_questions_on_connect():
    """If questions already exist when stream opens, emit them immediately."""
    user = _make_user()
    case = _make_case(ai_status=AiStatus.COMPLETED, question_round=0)
    db = _make_db_with_case(case)

    existing_question = {
        "question": "How long have you had this?",
        "answer_options": ["< 1 week", "> 1 week"],
        "question_index": 0,
    }

    with patch("src.conversations.stream._check_access", new=AsyncMock(return_value=case)), \
         patch("src.conversations.stream._get_ai_message_ids",
               new=AsyncMock(return_value={"msg-1"})), \
         patch("src.conversations.stream._get_current_questions",
               new=AsyncMock(return_value=[existing_question])), \
         patch("src.conversations.stream.POLL_INTERVAL_SECONDS", 0.01), \
         patch("src.conversations.stream.MAX_STREAM_SECONDS", 0.05):

        gen = chat_stream_generator(db, "case-1", user)
        events = await _collect(gen, limit=5)

    event_names = [e for e, _ in events]
    assert "questions_ready" in event_names

    q_event, q_data = next((e, d) for e, d in events if e == "questions_ready")
    assert q_data["round"] == 0
    assert len(q_data["questions"]) == 1
    assert q_data["questions"][0]["question"] == "How long have you had this?"


# ------------------------------------------------------------------ #
# Timeout auto-close
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_stream_closes_after_timeout():
    """Stream emits error and closes after MAX_STREAM_SECONDS."""
    db = AsyncMock()
    user = _make_user()
    case = _make_case()

    # Simulate case re-fetch returning same case each poll
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=case)
    ))

    with patch("src.conversations.stream._check_access", new=AsyncMock(return_value=case)), \
         patch("src.conversations.stream._get_ai_message_ids", new=AsyncMock(return_value=set())), \
         patch("src.conversations.stream._get_current_questions", new=AsyncMock(return_value=[])), \
         patch("src.conversations.stream.POLL_INTERVAL_SECONDS", 0.01), \
         patch("src.conversations.stream.MAX_STREAM_SECONDS", 0.05):

        gen = chat_stream_generator(db, "case-1", user)
        all_events = []
        async for chunk in gen:
            if chunk.strip():
                all_events.append(parse_sse(chunk))

    # Last event must be an error (timeout)
    last_event, last_data = all_events[-1]
    assert last_event == "error"
    assert "timeout" in last_data["message"].lower()


# ------------------------------------------------------------------ #
# New questions detected during poll
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_stream_emits_questions_ready_when_new_messages_appear():
    """When new AI messages appear during polling, questions_ready is emitted."""
    db = AsyncMock()
    user = _make_user()
    case = _make_case(ai_status=AiStatus.COMPLETED, question_round=0)

    new_question = {
        "question": "Is it itchy?",
        "answer_options": ["Yes", "No"],
        "question_index": 0,
    }

    call_count = 0

    async def mock_get_ai_ids(_db, _case_id):
        nonlocal call_count
        call_count += 1
        # First call (initial) returns empty, second call returns new message
        return set() if call_count == 1 else {"new-msg-1"}

    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=MagicMock(return_value=case)
    ))

    with patch("src.conversations.stream._check_access", new=AsyncMock(return_value=case)), \
         patch("src.conversations.stream._get_ai_message_ids", new=mock_get_ai_ids), \
         patch("src.conversations.stream._get_current_questions",
               new=AsyncMock(return_value=[new_question])), \
         patch("src.conversations.stream.POLL_INTERVAL_SECONDS", 0.01), \
         patch("src.conversations.stream.MAX_STREAM_SECONDS", 0.1):

        gen = chat_stream_generator(db, "case-1", user)
        events = await _collect(gen, limit=10)

    event_names = [e for e, _ in events]
    assert "questions_ready" in event_names


# ------------------------------------------------------------------ #
# Conversation complete
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_stream_closes_on_conversation_complete():
    """When round reaches max_rounds, conversation_complete is emitted and stream closes."""
    db = AsyncMock()
    user = _make_user()

    case_before = _make_case(question_round=2, max_question_rounds=3)
    case_complete = _make_case(
        question_round=3,
        max_question_rounds=3,
        case_summary="Likely psoriasis",
    )

    call_count = 0

    async def mock_execute(_stmt):
        nonlocal call_count
        call_count += 1
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(
            return_value=case_complete if call_count > 1 else case_before
        )
        return mock_result

    db.execute = mock_execute

    with patch("src.conversations.stream._check_access", new=AsyncMock(return_value=case_before)), \
         patch("src.conversations.stream._get_ai_message_ids", new=AsyncMock(return_value=set())), \
         patch("src.conversations.stream._get_current_questions", new=AsyncMock(return_value=[])), \
         patch("src.conversations.stream.POLL_INTERVAL_SECONDS", 0.01), \
         patch("src.conversations.stream.MAX_STREAM_SECONDS", 5.0):

        gen = chat_stream_generator(db, "case-1", user)
        all_events = []
        async for chunk in gen:
            if chunk.strip():
                all_events.append(parse_sse(chunk))

    event_names = [e for e, _ in all_events]
    assert "conversation_complete" in event_names

    complete_event, complete_data = next(
        (e, d) for e, d in all_events if e == "conversation_complete"
    )
    assert complete_data["case_summary"] == "Likely psoriasis"
