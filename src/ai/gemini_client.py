"""
ai/gemini_client.py — Gemini API Client
=========================================

Thin wrapper around the google-generativeai SDK.

WHY A WRAPPER?
--------------
Centralising Gemini calls here means:
- One place to swap models (gemini-2.0-flash → gemini-pro, etc.)
- One place to add retry logic, logging, and error normalisation
- Tests can mock `call_gemini` without touching the real SDK

IMAGES
------
Gemini's vision API accepts images as inline bytes with a MIME type.
We pass them as Part objects alongside the text prompt.
The model can handle multiple images in one call — we pass all case
images together so Gemini can reason across the full set.

GRACEFUL DEGRADATION
--------------------
If GEMINI_API_KEY is not set, `call_gemini` raises AIProviderException
with a clear message. The Celery task catches this and marks the case
as FAILED so the patient sees a meaningful error, not a crash.

JSON EXTRACTION
---------------
Gemini sometimes wraps JSON in markdown code fences (```json ... ```).
`extract_json()` strips these fences before `json.loads()`.
"""

import json
import re

from src.config import settings
from src.exceptions import AIProviderException
from src.logger import get_logger

logger = get_logger(__name__)


def _get_model():
    """
    Build and return a configured GenerativeModel.

    Model name is resolved at call time from the registry:
        Redis override  →  settings.GEMINI_MODEL  →  "gemini-2.0-flash"

    This means the admin panel can change the model without a restart.
    Raises AIProviderException if GEMINI_API_KEY is not set.
    """
    # Import here to avoid circular imports at module load time
    from src.ai.model_registry import get_gemini_model
    model_name = get_gemini_model()

    if not settings.GEMINI_API_KEY:
        raise AIProviderException(
            message="GEMINI_API_KEY is not configured. Set it in .env to enable AI analysis."
        )
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        return genai.GenerativeModel(model_name), model_name
    except ImportError:
        raise AIProviderException(
            message="google-generativeai package not installed. Run: pip install google-generativeai"
        )
    except Exception as exc:
        logger.error("gemini_client_init_failed", error=str(exc))
        raise AIProviderException(message=f"Failed to initialise Gemini client: {exc}") from exc


def call_gemini(prompt: str, images: list[bytes] | None = None, json_mode: bool = False) -> str:
    """
    Send a prompt (+ optional images) to Gemini and return the raw text response.

    Parameters
    ----------
    prompt     : The text instruction for Gemini
    images     : List of raw image bytes (JPEG, PNG, HEIC). Optional.
    json_mode  : When True, sets response_mime_type="application/json" so Gemini
                 always returns valid JSON with no markdown fences or prose preamble.

    Returns
    -------
    str — Gemini's raw text response (may contain JSON, markdown, etc.)

    Raises
    ------
    AIProviderException — API key missing, SDK not installed, or call failed
    """
    model, model_name = _get_model()

    try:
        import google.generativeai as genai

        parts: list = []

        # Attach images first so Gemini processes visual context before the prompt
        if images:
            for img_bytes in images:
                parts.append(
                    genai.protos.Part(
                        inline_data=genai.protos.Blob(
                            mime_type="image/jpeg",
                            data=img_bytes,
                        )
                    )
                )

        parts.append(prompt)

        generation_config = {"response_mime_type": "application/json"} if json_mode else {}

        response = model.generate_content(
            parts,
            generation_config=generation_config,
            request_options={"timeout": 45},
        )
        text = response.text
        logger.info(
            "gemini_call_ok",
            model=model_name,
            has_images=bool(images),
            image_count=len(images) if images else 0,
            response_length=len(text),
        )
        return text

    except AIProviderException:
        raise
    except Exception as exc:
        logger.error("gemini_call_failed", error=str(exc))
        raise AIProviderException(message=f"Gemini API call failed: {exc}") from exc


def extract_json(text: str) -> dict:
    """
    Parse JSON from Gemini's response, stripping markdown code fences if present.

    Gemini often wraps JSON in:
        ```json
        { ... }
        ```

    This function handles both bare JSON and fenced JSON.

    Parameters
    ----------
    text : Raw text from Gemini response

    Returns
    -------
    dict — parsed JSON object

    Raises
    ------
    AIProviderException — if the text cannot be parsed as JSON
    """
    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("gemini_json_parse_failed", raw_text=text[:200], error=str(exc))
        raise AIProviderException(
            message=f"Gemini returned invalid JSON: {exc}. Raw: {text[:200]!r}"
        ) from exc
