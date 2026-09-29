"""
tests/unit/test_llm_router.py — Multi-LLM Router Tests
=======================================================

Tests for:
    src/ai/openai_client.py   — call_openai, call_deepseek
    src/ai/llm_router.py      — call_llm fallback behaviour

APPROACH
--------
- All real HTTP calls are mocked — no actual API calls made
- Router fallback: verify provider order and fallback on failure
- Provider clients: verify correct SDK calls + exception wrapping
"""

from unittest.mock import MagicMock, patch

import pytest

from src.exceptions import AIProviderException


# ================================================================== #
# openai_client — call_openai
# ================================================================== #

def test_call_openai_returns_text():
    """call_openai returns the response text on success."""
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "Result text"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("src.ai.openai_client.settings") as mock_settings, \
         patch("src.ai.openai_client.OpenAI", return_value=mock_client):
        mock_settings.OPENAI_API_KEY = "sk-test"
        from src.ai.openai_client import call_openai
        result = call_openai("Describe this skin lesion.")

    assert result == "Result text"
    mock_client.chat.completions.create.assert_called_once()


def test_call_openai_raises_when_no_key():
    """call_openai raises AIProviderException when OPENAI_API_KEY is empty."""
    with patch("src.ai.openai_client.settings") as mock_settings:
        mock_settings.OPENAI_API_KEY = ""
        from src.ai.openai_client import call_openai
        with pytest.raises(AIProviderException, match="OPENAI_API_KEY"):
            call_openai("prompt")


def test_call_openai_wraps_sdk_exception():
    """SDK errors are wrapped in AIProviderException."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("connection refused")

    with patch("src.ai.openai_client.settings") as mock_settings, \
         patch("src.ai.openai_client.OpenAI", return_value=mock_client):
        mock_settings.OPENAI_API_KEY = "sk-test"
        from src.ai.openai_client import call_openai
        with pytest.raises(AIProviderException, match="OpenAI API call failed"):
            call_openai("prompt")


def test_call_openai_encodes_images_as_base64():
    """Images are passed as base64 data URIs in the message content."""
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "ok"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("src.ai.openai_client.settings") as mock_settings, \
         patch("src.ai.openai_client.OpenAI", return_value=mock_client):
        mock_settings.OPENAI_API_KEY = "sk-test"
        from src.ai.openai_client import call_openai
        call_openai("prompt", images=[b"\xff\xd8\xff"])  # fake JPEG bytes

    call_args = mock_client.chat.completions.create.call_args
    messages = call_args[1]["messages"]
    content = messages[0]["content"]
    # First item: text, second item: image_url
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


# ================================================================== #
# openai_client — call_deepseek
# ================================================================== #

def test_call_deepseek_returns_text():
    """call_deepseek returns the response text on success."""
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "DeepSeek answer"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("src.ai.openai_client.settings") as mock_settings, \
         patch("src.ai.openai_client.OpenAI", return_value=mock_client):
        mock_settings.DEEPSEEK_API_KEY = "ds-test"
        from src.ai.openai_client import call_deepseek
        result = call_deepseek("prompt")

    assert result == "DeepSeek answer"


def test_call_deepseek_raises_when_no_key():
    """call_deepseek raises AIProviderException when DEEPSEEK_API_KEY is empty."""
    with patch("src.ai.openai_client.settings") as mock_settings:
        mock_settings.DEEPSEEK_API_KEY = ""
        from src.ai.openai_client import call_deepseek
        with pytest.raises(AIProviderException, match="DEEPSEEK_API_KEY"):
            call_deepseek("prompt")


def test_call_deepseek_adds_image_note_to_prompt():
    """When images are passed, a note is prepended (DeepSeek is text-only)."""
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "ok"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("src.ai.openai_client.settings") as mock_settings, \
         patch("src.ai.openai_client.OpenAI", return_value=mock_client):
        mock_settings.DEEPSEEK_API_KEY = "ds-test"
        from src.ai.openai_client import call_deepseek
        call_deepseek("base prompt", images=[b"\x89PNG"])

    call_args = mock_client.chat.completions.create.call_args
    messages = call_args[1]["messages"]
    sent_content = messages[0]["content"]
    assert "1 medical image" in sent_content
    assert "base prompt" in sent_content


# ================================================================== #
# llm_router — call_llm
# ================================================================== #

def test_call_llm_uses_primary_provider():
    """Router calls the primary provider (gemini by default) and returns result."""
    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini", return_value="gemini result"):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        result = call_llm("prompt")

    assert result == "gemini result"


def test_call_llm_falls_back_to_openai_when_gemini_fails():
    """Router falls back to OpenAI when Gemini raises AIProviderException."""
    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini",
               side_effect=AIProviderException(message="quota exceeded")), \
         patch("src.ai.llm_router.openai_client.call_openai", return_value="openai result"):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        result = call_llm("prompt")

    assert result == "openai result"


def test_call_llm_falls_back_to_deepseek_when_gemini_and_openai_fail():
    """Router falls back to DeepSeek when both Gemini and OpenAI fail."""
    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini",
               side_effect=AIProviderException(message="gemini down")), \
         patch("src.ai.llm_router.openai_client.call_openai",
               side_effect=AIProviderException(message="openai down")), \
         patch("src.ai.llm_router.openai_client.call_deepseek", return_value="deepseek result"):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        result = call_llm("prompt")

    assert result == "deepseek result"


def test_call_llm_raises_when_all_providers_fail():
    """Router raises AIProviderException summarising all failures."""
    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini",
               side_effect=AIProviderException(message="gemini down")), \
         patch("src.ai.llm_router.openai_client.call_openai",
               side_effect=AIProviderException(message="openai down")), \
         patch("src.ai.llm_router.openai_client.call_deepseek",
               side_effect=AIProviderException(message="deepseek down")):
        mock_settings.DEFAULT_LLM_PROVIDER = "gemini"
        from src.ai.llm_router import call_llm
        with pytest.raises(AIProviderException, match="All LLM providers failed"):
            call_llm("prompt")


def test_call_llm_respects_default_provider_openai():
    """When DEFAULT_LLM_PROVIDER=openai, OpenAI is tried first."""
    openai_mock = MagicMock(return_value="openai first")

    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.openai_client.call_openai", openai_mock), \
         patch("src.ai.llm_router.gemini_client.call_gemini") as gemini_mock:
        mock_settings.DEFAULT_LLM_PROVIDER = "openai"
        from src.ai.llm_router import call_llm
        result = call_llm("prompt")

    assert result == "openai first"
    gemini_mock.assert_not_called()


def test_call_llm_respects_default_provider_deepseek():
    """When DEFAULT_LLM_PROVIDER=deepseek, DeepSeek is tried first."""
    deepseek_mock = MagicMock(return_value="deepseek first")

    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.openai_client.call_deepseek", deepseek_mock), \
         patch("src.ai.llm_router.gemini_client.call_gemini") as gemini_mock, \
         patch("src.ai.llm_router.openai_client.call_openai") as openai_mock:
        mock_settings.DEFAULT_LLM_PROVIDER = "deepseek"
        from src.ai.llm_router import call_llm
        result = call_llm("prompt")

    assert result == "deepseek first"
    gemini_mock.assert_not_called()
    openai_mock.assert_not_called()


def test_call_llm_unknown_provider_defaults_to_gemini_order():
    """Unknown DEFAULT_LLM_PROVIDER falls back to gemini order."""
    with patch("src.ai.llm_router.settings") as mock_settings, \
         patch("src.ai.llm_router.gemini_client.call_gemini", return_value="ok"):
        mock_settings.DEFAULT_LLM_PROVIDER = "unknown_provider"
        from src.ai.llm_router import call_llm
        result = call_llm("prompt")

    assert result == "ok"
