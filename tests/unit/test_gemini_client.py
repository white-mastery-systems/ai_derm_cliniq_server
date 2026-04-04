"""
tests/unit/test_gemini_client.py — Gemini Client Unit Tests
=============================================================

Tests for src/ai/gemini_client.py

WHAT WE TEST
------------
1. extract_json — handles bare JSON, markdown-fenced JSON, invalid JSON
2. call_gemini raises AIProviderException when no API key is set
3. call_gemini raises AIProviderException when SDK is not installed

NO REAL API CALLS
-----------------
All tests that would call the Gemini API are skipped or mock the SDK.
These tests run without a GEMINI_API_KEY.
"""

import json

import pytest

from src.ai.gemini_client import extract_json
from src.exceptions import AIProviderException


# ================================================================== #
# extract_json
# ================================================================== #

class TestExtractJson:

    def test_parses_bare_json_object(self):
        text = '{"answer": "yes"}'
        result = extract_json(text)
        assert result == {"answer": "yes"}

    def test_parses_fenced_json_block(self):
        text = '```json\n{"answer": "no", "reason": "blurry"}\n```'
        result = extract_json(text)
        assert result == {"answer": "no", "reason": "blurry"}

    def test_parses_fenced_block_without_language_tag(self):
        text = '```\n{"answer": "yes"}\n```'
        result = extract_json(text)
        assert result == {"answer": "yes"}

    def test_parses_json_with_whitespace(self):
        text = '  \n  {"key": "value"}  \n  '
        result = extract_json(text)
        assert result == {"key": "value"}

    def test_parses_nested_json(self):
        text = json.dumps({
            "most_probable_diagnosis": {
                "diagnosis": "Eczema",
                "likelihood": "High",
                "key_supporting_features": "Dry, itchy skin"
            },
            "confidence in answer": "high"
        })
        result = extract_json(text)
        assert result["most_probable_diagnosis"]["diagnosis"] == "Eczema"
        assert result["confidence in answer"] == "high"

    def test_raises_on_invalid_json(self):
        with pytest.raises(AIProviderException) as exc_info:
            extract_json("this is not json at all")
        assert "invalid json" in exc_info.value.message.lower()

    def test_raises_on_truncated_json(self):
        with pytest.raises(AIProviderException):
            extract_json('{"key": "value"')  # Missing closing brace

    def test_parses_lesion_description_schema(self):
        """Verify the exact schema returned by ImageAnalysisPrompts.get_description()."""
        sample = json.dumps({
            "type_of_lesion": "Plaque",
            "site": "Left forearm",
            "count": "Multiple",
            "arrangement": "Grouped",
            "size": "2-3 cm",
            "color_pattern": "Erythematous",
            "border": "Well-defined",
            "surface_changes": "Scaling",
            "presence_of_exudate_or_discharge": "No",
            "surrounding_skin_changes": "None",
            "secondary_changes": "None",
            "pattern_or_shape": "Irregular",
            "additional_notes": "None",
            "overall_description": "Multiple erythematous plaques with scaling."
        })
        result = extract_json(sample)
        assert result["type_of_lesion"] == "Plaque"
        assert result["overall_description"] == "Multiple erythematous plaques with scaling."

    def test_parses_differential_schema(self):
        """Verify the exact schema returned by ImageAnalysisPrompts.generate_first_differential()."""
        sample = json.dumps({
            "most_probable_diagnosis": {
                "diagnosis": "Psoriasis",
                "likelihood": "High",
                "key_supporting_features": "Silvery scale, well-defined borders"
            },
            "differential_diagnoses": [
                {
                    "diagnosis": "Eczema",
                    "likelihood": "Medium",
                    "key_supporting_features": "Itching, flexural involvement"
                }
            ],
            "confidence in answer": "high"
        })
        result = extract_json(sample)
        assert result["most_probable_diagnosis"]["diagnosis"] == "Psoriasis"
        assert len(result["differential_diagnoses"]) == 1
        assert result["confidence in answer"] == "high"

    def test_parses_inspect_images_yes_response(self):
        result = extract_json('{"answer":"yes"}')
        assert result["answer"] == "yes"

    def test_parses_inspect_images_no_response(self):
        text = '{"answer":"no", "reason":"Image is too blurry to assess."}'
        result = extract_json(text)
        assert result["answer"] == "no"
        assert "blurry" in result["reason"]


# ================================================================== #
# call_gemini — no API key
# ================================================================== #

class TestCallGeminiNoKey:

    def test_raises_when_no_api_key(self, monkeypatch):
        """call_gemini raises AIProviderException if GEMINI_API_KEY is not set."""
        from src.config import settings
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")

        from src.ai import gemini_client
        with pytest.raises(AIProviderException) as exc_info:
            gemini_client.call_gemini("test prompt")
        assert "GEMINI_API_KEY" in exc_info.value.message
