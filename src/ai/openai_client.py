"""
ai/openai_client.py — OpenAI-compatible API Client
====================================================

Handles both OpenAI (GPT-4o) and DeepSeek (deepseek-chat) because
DeepSeek uses the same REST API schema as OpenAI — just a different
base URL and model name.

WHY A SEPARATE FILE FROM gemini_client.py?
-------------------------------------------
Gemini uses google-generativeai SDK with its own Part/Blob types for
images. OpenAI uses the standard openai SDK with base64-encoded image
URLs in the message content. Keeping them separate avoids conflating
two very different image encoding strategies.

IMAGE ENCODING
--------------
OpenAI vision: images are passed as data URIs inside the message:
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}

DeepSeek vision: same schema via OpenAI-compatible endpoint.

GRACEFUL DEGRADATION
---------------------
If the API key is not set, the call raises AIProviderException.
The llm_router catches this and tries the next provider.

JSON EXTRACTION
---------------
Same fence-stripping logic as gemini_client — LLMs consistently wrap
JSON in markdown code blocks.
"""

import base64
import json
import re

from src.config import settings
from src.exceptions import AIProviderException
from src.logger import get_logger

logger = get_logger(__name__)

# Module-level import so tests can patch `src.ai.openai_client.OpenAI`
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]

# ------------------------------------------------------------------ #
# OpenAI
# ------------------------------------------------------------------ #

def call_openai(prompt: str, images: list[bytes] | None = None, json_mode: bool = False) -> str:
    """
    Send a prompt (+ optional images) to OpenAI GPT-4o.

    Parameters
    ----------
    prompt    : str   — Text instruction
    images    : list  — Raw image bytes (JPEG/PNG). Optional.
    json_mode : bool  — When True, sets response_format=json_object to force
                        valid JSON output with no markdown fences.

    Returns
    -------
    str — Raw text response from GPT-4o

    Raises
    ------
    AIProviderException — key missing, SDK not installed, or call failed
    """
    from src.ai.model_registry import get_openai_model
    model_name = get_openai_model()

    if not settings.OPENAI_API_KEY:
        raise AIProviderException(
            message="OPENAI_API_KEY is not configured. Set it in .env to enable OpenAI fallback."
        )

    if OpenAI is None:
        raise AIProviderException(
            message="openai package not installed. Run: pip install openai"
        )

    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    # Build message content
    content: list[dict] = [{"type": "text", "text": prompt}]

    if images:
        for img_bytes in images:
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })

    kwargs: dict = {"model": model_name, "messages": [{"role": "user", "content": content}], "max_tokens": 4096}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        response = client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
        logger.info(
            "openai_call_ok",
            model=model_name,
            has_images=bool(images),
            response_length=len(text),
        )
        return text

    except AIProviderException:
        raise
    except Exception as exc:
        logger.error("openai_call_failed", error=str(exc))
        raise AIProviderException(message=f"OpenAI API call failed: {exc}") from exc


# ------------------------------------------------------------------ #
# DeepSeek
# ------------------------------------------------------------------ #
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def call_deepseek(prompt: str, images: list[bytes] | None = None, json_mode: bool = False) -> str:
    """
    Send a prompt to DeepSeek via its OpenAI-compatible API.

    DeepSeek's vision support is text-only on deepseek-chat.
    Images are described textually in the prompt if provided.
    (deepseek-vl is available but requires a separate endpoint.)

    Parameters
    ----------
    prompt    : str   — Text instruction
    images    : list  — Ignored (DeepSeek chat is text-only in this config)
    json_mode : bool  — When True, sets response_format=json_object.

    Returns
    -------
    str — Raw text response from DeepSeek

    Raises
    ------
    AIProviderException — key missing, SDK not installed, or call failed
    """
    from src.ai.model_registry import get_deepseek_model
    model_name = get_deepseek_model()

    if not settings.DEEPSEEK_API_KEY:
        raise AIProviderException(
            message="DEEPSEEK_API_KEY is not configured. Set it in .env to enable DeepSeek fallback."
        )

    if OpenAI is None:
        raise AIProviderException(
            message="openai package not installed. Run: pip install openai"
        )

    # DeepSeek uses OpenAI-compatible endpoint
    client = OpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=_DEEPSEEK_BASE_URL,
    )

    # DeepSeek chat: text-only (no vision in base model)
    # If images were passed, note it in the prompt for context
    full_prompt = prompt
    if images:
        full_prompt = (
            f"[Note: {len(images)} medical image(s) were provided but cannot be "
            f"directly processed. Please respond based on the text prompt only.]\n\n{prompt}"
        )

    ds_kwargs: dict = {"model": model_name, "messages": [{"role": "user", "content": full_prompt}], "max_tokens": 4096}
    if json_mode:
        ds_kwargs["response_format"] = {"type": "json_object"}

    try:
        response = client.chat.completions.create(**ds_kwargs)
        text = response.choices[0].message.content or ""
        logger.info(
            "deepseek_call_ok",
            model=model_name,
            response_length=len(text),
        )
        return text

    except AIProviderException:
        raise
    except Exception as exc:
        logger.error("deepseek_call_failed", error=str(exc))
        raise AIProviderException(message=f"DeepSeek API call failed: {exc}") from exc


# ------------------------------------------------------------------ #
# JSON extraction (shared with gemini_client pattern)
# ------------------------------------------------------------------ #

def extract_json(text: str) -> dict:
    """
    Parse JSON from an LLM response, stripping markdown code fences.

    Same logic as gemini_client.extract_json — kept local to avoid
    circular imports between client modules.
    """
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip()).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("openai_json_parse_failed", raw_text=text[:200], error=str(exc))
        raise AIProviderException(
            message=f"LLM returned invalid JSON: {exc}. Raw: {text[:200]!r}"
        ) from exc
