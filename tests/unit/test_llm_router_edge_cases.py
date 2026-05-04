"""
tests/unit/test_llm_router_edge_cases.py — LLM Router Edge-Case Tests
=====================================================================

Edge cases NOT covered by test_llm_router.py:

1. json_mode=True is forwarded to the provider call function
2. Non-AIProviderException from a provider bubbles up immediately (no fallback)
3. Images survive through the fallback chain (Gemini fails, OpenAI gets the images)
4. DeepSeek-as-last-resort with images — still succeeds (text-only path)
5. Error summary includes all three provider names when all fail
6. openai-primary → gemini fallback path (non-default ordering)
7. deepseek-primary ordering skips both vision providers if it succeeds
8. Empty images list behaves the same as None (not treated as "has images")
"""

from unittest.mock import MagicMock, call, patch

import pytest

from src.exceptions import AIProviderException


# ================================================================== #
# 1. json_mode forwarded to provider
# ================================================================== #

def test_call_llm_forwards_json_mode_true_to_provider():
    """json_mode=True must reach the provider call function."""
    captured = {}

    def fake_gemini(prompt, images, json_mode):
        captured["json_mode"] = json_mode
        return "ok"

    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini", side_effect=fake_gemini):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        call_llm("prompt", json_mode=True)

    assert captured["json_mode"] is True


def test_call_llm_forwards_json_mode_false_by_default():
    """json_mode defaults to False if not specified."""
    captured = {}

    def fake_gemini(prompt, images, json_mode):
        captured["json_mode"] = json_mode
        return "ok"

    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini", side_effect=fake_gemini):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        call_llm("prompt")

    assert captured["json_mode"] is False


# ================================================================== #
# 2. Non-AIProviderException bubbles up immediately (no fallback)
# ================================================================== #

def test_call_llm_non_provider_exception_is_not_caught():
    """
    An unexpected RuntimeError from a provider must NOT be silently swallowed.
    The router only catches AIProviderException; other errors propagate.
    This guards against masking programming bugs (e.g. misconfigured clients).
    """
    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini",
               side_effect=RuntimeError("unexpected crash")), \
         patch("src.ai.llm_router.openai_client.call_openai") as openai_mock:
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        with pytest.raises(RuntimeError, match="unexpected crash"):
            call_llm("prompt")

    # OpenAI must NOT have been called — error stops at Gemini
    openai_mock.assert_not_called()


# ================================================================== #
# 3. Images survive the fallback chain to OpenAI
# ================================================================== #

def test_call_llm_images_forwarded_to_fallback_provider():
    """
    When Gemini fails, the same images bytes must be forwarded to OpenAI.
    Vision data must not be lost during fallback.
    """
    fake_image = b"\xff\xd8\xff"  # minimal JPEG header
    received = {}

    def fake_openai(prompt, images, json_mode):
        received["images"] = images
        return "openai result"

    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini",
               side_effect=AIProviderException(message="gemini quota")), \
         patch("src.ai.llm_router.openai_client.call_openai", side_effect=fake_openai):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        result = call_llm("diagnose", images=[fake_image])

    assert result == "openai result"
    assert received["images"] == [fake_image]


# ================================================================== #
# 4. DeepSeek last-resort with images (text-only degradation)
# ================================================================== #

def test_call_llm_deepseek_last_resort_with_images_succeeds():
    """
    When both vision-capable providers fail, DeepSeek (text-only) is tried.
    call_deepseek handles images internally by embedding a note — the router
    still succeeds, just with degraded (text-only) AI reasoning.
    """
    fake_image = b"\x89PNG"
    received = {}

    def fake_deepseek(prompt, images, json_mode):
        received["images"] = images
        return "deepseek text-only result"

    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini",
               side_effect=AIProviderException(message="gemini down")), \
         patch("src.ai.llm_router.openai_client.call_openai",
               side_effect=AIProviderException(message="openai down")), \
         patch("src.ai.llm_router.openai_client.call_deepseek", side_effect=fake_deepseek):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        result = call_llm("diagnose rash", images=[fake_image])

    assert result == "deepseek text-only result"
    # Images were passed to deepseek — it handles them internally
    assert received["images"] == [fake_image]


# ================================================================== #
# 5. Error summary includes all three provider names
# ================================================================== #

def test_call_llm_all_fail_error_message_names_all_providers():
    """
    When all providers fail, the exception message must mention all three
    so that log aggregation tools can surface which providers were tried.
    """
    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini",
               side_effect=AIProviderException(message="quota exceeded")), \
         patch("src.ai.llm_router.openai_client.call_openai",
               side_effect=AIProviderException(message="rate limited")), \
         patch("src.ai.llm_router.openai_client.call_deepseek",
               side_effect=AIProviderException(message="service unavailable")):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        with pytest.raises(AIProviderException) as exc_info:
            call_llm("prompt")

    msg = str(exc_info.value)
    assert "Gemini" in msg or "gemini" in msg.lower()
    assert "GPT" in msg or "openai" in msg.lower()
    assert "DeepSeek" in msg or "deepseek" in msg.lower()
    assert "quota exceeded" in msg
    assert "rate limited" in msg
    assert "service unavailable" in msg


# ================================================================== #
# 6. openai-primary → gemini fallback (non-default ordering)
# ================================================================== #

def test_call_llm_openai_primary_falls_back_to_gemini():
    """
    When DEFAULT_LLM_PROVIDER=openai and OpenAI fails,
    the fallback is Gemini (not DeepSeek).
    This tests a non-gemini-primary ordering path.
    """
    gemini_mock = MagicMock(return_value="gemini fallback result")

    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.openai_client.call_openai",
               side_effect=AIProviderException(message="openai down")), \
         patch("src.ai.llm_router.gemini_client.call_gemini", gemini_mock), \
         patch("src.ai.llm_router.openai_client.call_deepseek") as deepseek_mock:
        mock_settings.DEFAULT_LLM_PROVIDER = "openai"
        from src.ai.llm_router import call_llm
        result = call_llm("prompt")

    assert result == "gemini fallback result"
    deepseek_mock.assert_not_called()


# ================================================================== #
# 7. deepseek-primary succeeds without touching vision providers
# ================================================================== #

def test_call_llm_deepseek_primary_does_not_call_vision_providers():
    """
    When DEFAULT_LLM_PROVIDER=deepseek and DeepSeek succeeds,
    Gemini and OpenAI must never be called (no unnecessary API calls).
    """
    deepseek_mock = MagicMock(return_value="deepseek ok")

    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.openai_client.call_deepseek", deepseek_mock), \
         patch("src.ai.llm_router.gemini_client.call_gemini") as gemini_mock, \
         patch("src.ai.llm_router.openai_client.call_openai") as openai_mock:
        mock_settings.DEFAULT_LLM_PROVIDER = "deepseek"
        from src.ai.llm_router import call_llm
        result = call_llm("text prompt")

    assert result == "deepseek ok"
    gemini_mock.assert_not_called()
    openai_mock.assert_not_called()


# ================================================================== #
# 8. Empty images list is passed through (not treated as "no images")
# ================================================================== #

def test_call_llm_empty_images_list_is_passed_through():
    """
    An empty list [] for images is different from None.
    The router passes whatever it receives to the provider — not its concern
    to pre-process. The provider decides how to handle [].
    """
    received = {}

    def fake_gemini(prompt, images, json_mode):
        received["images"] = images
        return "ok"

    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini", side_effect=fake_gemini):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        call_llm("prompt", images=[])

    assert received["images"] == []
