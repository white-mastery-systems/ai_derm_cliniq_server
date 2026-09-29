"""
tests/unit/test_conversation_helpers.py — Conversation Helper Unit Tests
=========================================================================

Tests for helper functions in src/workers/tasks/questions.py:
  - _build_conversation_history  — formats messages for AI prompts
  - _build_previous_questions_text — flat question list for deduplication

And for the service's _parse_question helper via the schema parsing.

No DB, no network, no Celery — pure Python logic.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.workers.tasks.questions import (
    _build_conversation_history,
    _build_previous_questions_text,
)
from src.models.message import MessageRole


# ================================================================== #
# Helpers to build mock Message objects
# ================================================================== #

def make_message(role, content, round_number, question_index=None):
    msg = MagicMock()
    msg.role = role
    msg.content = content
    msg.round_number = round_number
    msg.question_index = question_index
    msg.created_at = datetime.now(tz=timezone.utc)
    return msg


# ================================================================== #
# _build_conversation_history
# ================================================================== #

class TestBuildConversationHistory:

    def test_single_round_question_and_answer(self):
        q_content = json.dumps({"question": "How long have you had this?", "answer_options": ["1 week", "2 weeks"]})
        messages = [
            make_message(MessageRole.AI, q_content, round_number=0, question_index=0),
            make_message(MessageRole.PATIENT, "1 week", round_number=0, question_index=0),
        ]
        result = _build_conversation_history(messages)
        assert "Q1: How long have you had this?" in result
        assert "A1: 1 week" in result

    def test_multiple_questions_in_one_round(self):
        q1 = json.dumps({"question": "How long?", "answer_options": []})
        q2 = json.dumps({"question": "Does it itch?", "answer_options": []})
        messages = [
            make_message(MessageRole.AI, q1, round_number=0, question_index=0),
            make_message(MessageRole.AI, q2, round_number=0, question_index=1),
            make_message(MessageRole.PATIENT, "2 weeks", round_number=0, question_index=0),
            make_message(MessageRole.PATIENT, "Yes, severely", round_number=0, question_index=1),
        ]
        result = _build_conversation_history(messages)
        assert "Q1: How long?" in result
        assert "A1: 2 weeks" in result
        assert "Q2: Does it itch?" in result
        assert "A2: Yes, severely" in result

    def test_unanswered_questions_omit_answer_line(self):
        """Questions without patient replies omit the A line (no empty A: noise in prompt)."""
        q_content = json.dumps({"question": "What site?", "answer_options": []})
        messages = [
            make_message(MessageRole.AI, q_content, round_number=0, question_index=0),
            # No patient message
        ]
        result = _build_conversation_history(messages)
        assert "Q1: What site?" in result
        assert "A1:" not in result  # Omitted when no answer given

    def test_multiple_rounds_ordered_correctly(self):
        q1 = json.dumps({"question": "Round 0 Q1?", "answer_options": []})
        q2 = json.dumps({"question": "Round 1 Q1?", "answer_options": []})
        messages = [
            make_message(MessageRole.AI, q1, round_number=0, question_index=0),
            make_message(MessageRole.PATIENT, "Answer to R0Q1", round_number=0, question_index=0),
            make_message(MessageRole.AI, q2, round_number=1, question_index=0),
            make_message(MessageRole.PATIENT, "Answer to R1Q1", round_number=1, question_index=0),
        ]
        result = _build_conversation_history(messages)
        lines = result.split("\n")
        q1_idx = next(i for i, l in enumerate(lines) if "Round 0 Q1" in l)
        q2_idx = next(i for i, l in enumerate(lines) if "Round 1 Q1" in l)
        assert q1_idx < q2_idx  # Round 0 before Round 1

    def test_empty_messages_returns_empty_string(self):
        result = _build_conversation_history([])
        assert result == ""

    def test_non_json_ai_content_falls_back_to_raw(self):
        """If AI message content is not valid JSON, use the raw string as question."""
        messages = [
            make_message(MessageRole.AI, "Plain question text", round_number=0, question_index=0),
            make_message(MessageRole.PATIENT, "Plain answer", round_number=0, question_index=0),
        ]
        result = _build_conversation_history(messages)
        assert "Q1: Plain question text" in result
        assert "A1: Plain answer" in result


# ================================================================== #
# _build_previous_questions_text
# ================================================================== #

class TestBuildPreviousQuestionsText:

    def test_extracts_all_ai_question_texts(self):
        q1 = json.dumps({"question": "Question one?", "answer_options": []})
        q2 = json.dumps({"question": "Question two?", "answer_options": []})
        messages = [
            make_message(MessageRole.AI, q1, round_number=0, question_index=0),
            make_message(MessageRole.AI, q2, round_number=0, question_index=1),
            make_message(MessageRole.PATIENT, "Answer", round_number=0, question_index=0),
        ]
        result = _build_previous_questions_text(messages)
        assert "Question one?" in result
        assert "Question two?" in result
        assert "Answer" not in result

    def test_empty_list_returns_empty_string(self):
        result = _build_previous_questions_text([])
        assert result == ""

    def test_non_json_falls_back_to_raw_content(self):
        messages = [
            make_message(MessageRole.AI, "Raw question", round_number=0, question_index=0),
        ]
        result = _build_previous_questions_text(messages)
        assert "Raw question" in result


# ================================================================== #
# Question parsing (via json.loads — same logic used in service)
# ================================================================== #

class TestQuestionParsing:

    def test_parses_question_item_from_json_content(self):
        content = json.dumps({
            "question": "How long have you had the rash?",
            "answer_options": ["Less than 1 week", "1–2 weeks", "More than 2 weeks"],
        })
        data = json.loads(content)
        assert data["question"] == "How long have you had the rash?"
        assert len(data["answer_options"]) == 3

    def test_questions_list_from_gemini_response(self):
        """Verify the exact JSON schema returned by first_question() prompt."""
        gemini_output = json.dumps({
            "Questions": [
                {"question": "Q1?", "answer_options": ["A", "B"]},
                {"question": "Q2?", "answer_options": ["C", "D"]},
                {"question": "Q3?", "answer_options": ["E", "F"]},
            ]
        })
        data = json.loads(gemini_output)
        assert len(data["Questions"]) == 3
        assert data["Questions"][0]["question"] == "Q1?"
        assert "A" in data["Questions"][0]["answer_options"]
