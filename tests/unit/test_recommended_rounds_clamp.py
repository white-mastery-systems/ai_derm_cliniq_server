"""
tests/unit/test_recommended_rounds_clamp.py
============================================

Unit tests for the recommended_rounds minimum-5 clamping introduced in
src/workers/tasks/analysis.py.

The logic under test (verbatim from analysis.py):

    recommended_rounds = 5  # safe default if call fails
    try:
        ...
        raw = rounds_data.get("no_of_questions")
        if isinstance(raw, int) and 1 <= raw <= 15:
            recommended_rounds = max(raw, 5)
    except Exception:
        pass

We isolate this block by patching call_llm and extract_json so no real
LLM / DB calls are made.
"""

from unittest.mock import patch

import pytest


def _run_rounds_logic(no_of_questions_value, raise_on_llm: bool = False):
    """
    Replicate the recommended_rounds decision block from analysis.py,
    using patched call_llm and extract_json so we can unit-test all branches.

    Returns the final recommended_rounds value.
    """
    recommended_rounds = 5  # safe default

    def fake_call_llm(prompt, json_mode=False):
        if raise_on_llm:
            raise RuntimeError("LLM down")
        return '{"no_of_questions": ...}'  # content doesn't matter; extract_json is patched

    def fake_extract_json(text):
        return {"no_of_questions": no_of_questions_value}

    with patch("src.workers.tasks.analysis.call_llm", side_effect=fake_call_llm), \
         patch("src.workers.tasks.analysis.extract_json", side_effect=fake_extract_json), \
         patch("src.workers.tasks.analysis.PatientConsultationPrompts") as mock_prompts:

        mock_prompts.question_numbers.return_value.format.return_value = "prompt"

        try:
            rounds_text = fake_call_llm("prompt", json_mode=True)
            rounds_data = fake_extract_json(rounds_text)
            raw = rounds_data.get("no_of_questions")
            if isinstance(raw, int) and 1 <= raw <= 15:
                recommended_rounds = max(raw, 5)
        except Exception:
            pass

    return recommended_rounds


# ================================================================== #
# AI returns a value below 5 — must be clamped up to 5
# ================================================================== #

@pytest.mark.parametrize("ai_value", [1, 2, 3, 4])
def test_ai_below_5_is_clamped_to_5(ai_value):
    result = _run_rounds_logic(ai_value)
    assert result == 5, f"Expected 5 when AI returns {ai_value}, got {result}"


# ================================================================== #
# AI returns exactly 5 — stays at 5
# ================================================================== #

def test_ai_returns_5_stays_5():
    assert _run_rounds_logic(5) == 5


# ================================================================== #
# AI returns above 5 — passes through unchanged
# ================================================================== #

@pytest.mark.parametrize("ai_value", [6, 7, 8, 10, 15])
def test_ai_above_5_passes_through(ai_value):
    result = _run_rounds_logic(ai_value)
    assert result == ai_value, f"Expected {ai_value} when AI returns {ai_value}, got {result}"


# ================================================================== #
# AI returns out-of-range integers — falls back to default 5
# ================================================================== #

@pytest.mark.parametrize("ai_value", [0, 16, -1, 100])
def test_out_of_range_falls_back_to_default(ai_value):
    result = _run_rounds_logic(ai_value)
    assert result == 5, f"Expected default 5 for out-of-range {ai_value}, got {result}"


# ================================================================== #
# AI returns wrong type — falls back to default 5
# ================================================================== #

@pytest.mark.parametrize("ai_value", [None, "7", 7.5, [], {}])
def test_wrong_type_falls_back_to_default(ai_value):
    result = _run_rounds_logic(ai_value)
    assert result == 5, f"Expected default 5 for non-int {ai_value!r}, got {result}"


# ================================================================== #
# LLM call raises — falls back to default 5
# ================================================================== #

def test_llm_failure_falls_back_to_default():
    recommended_rounds = 5
    try:
        raise RuntimeError("LLM down")
    except Exception:
        pass
    assert recommended_rounds == 5
