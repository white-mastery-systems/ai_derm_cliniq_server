"""
ai/gemini_client.py — Gemini API Client
=========================================

Thin wrapper around the google-genai SDK (v1.x).

WHY A WRAPPER?
--------------
Centralising Gemini calls here means:
- One place to swap models (gemini-2.5-flash → gemini-pro, etc.)
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

THINKING
--------
gemini-2.5-flash enables thinking by default (adds 2000-4000 hidden
tokens per call, increasing latency by 10-30s). We disable it via
ThinkingConfig(thinking_budget=0) — requires the new google-genai SDK.
"""

import json
import re

from src.config import settings
from src.exceptions import AIProviderException
from src.logger import get_logger

logger = get_logger(__name__)

# Gemini finish_reason → human-readable name for logs
_FINISH_REASON_NAMES: dict[str, str] = {
    "FINISH_REASON_UNSPECIFIED": "UNSPECIFIED",
    "STOP":        "STOP",        # normal completion
    "MAX_TOKENS":  "MAX_TOKENS",  # hit output token limit — JSON will be truncated
    "SAFETY":      "SAFETY",      # blocked by safety filter
    "RECITATION":  "RECITATION",  # blocked for recitation
    "OTHER":       "OTHER",
    "LANGUAGE":    "LANGUAGE",
    "BLOCKLIST":   "BLOCKLIST",
}

# Normal finish reasons — anything else means early termination
_OK_FINISH_REASONS = {"FINISH_REASON_UNSPECIFIED", "STOP"}


def _get_client_and_model() -> tuple:
    """
    Build and return a (Client, model_name) tuple.

    Model name is resolved at call time from the registry:
        Redis override  →  settings.GEMINI_MODEL  →  "gemini-2.5-flash"

    Raises AIProviderException if GEMINI_API_KEY is not set.
    """
    from src.ai.model_registry import get_gemini_model
    model_name = get_gemini_model()

    if not settings.GEMINI_API_KEY:
        raise AIProviderException(
            message="GEMINI_API_KEY is not configured. Set it in .env to enable AI analysis."
        )
    try:
        from google import genai
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return client, model_name
    except ImportError:
        raise AIProviderException(
            message="google-genai package not installed. Run: pip install google-genai"
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
    client, model_name = _get_client_and_model()

    try:
        from google import genai
        from google.genai import types

        contents: list = []

        # Attach images first so Gemini processes visual context before the prompt
        if images:
            for img_bytes in images:
                contents.append(
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="image/jpeg",
                            data=img_bytes,
                        )
                    )
                )

        contents.append(prompt)

        config = types.GenerateContentConfig(
            max_output_tokens=8192,
            # Disable thinking — saves 2000-4000 tokens and 10-30s per call.
            # gemini-2.5-flash enables thinking by default; budget=0 turns it off.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            response_mime_type="application/json" if json_mode else None,
        )

        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )

        candidate = response.candidates[0] if response.candidates else None

        # Token usage
        usage = response.usage_metadata
        prompt_tokens = usage.prompt_token_count     if usage else None
        output_tokens = usage.candidates_token_count if usage else None
        total_tokens  = usage.total_token_count      if usage else None

        # finish_reason is a FinishReason enum — use .name for the string key
        finish_reason_enum = candidate.finish_reason if candidate else None
        finish_reason_name = finish_reason_enum.name if finish_reason_enum else "UNKNOWN"

        logger.debug(
            "gemini_debug",
            model=model_name,
            has_images=bool(images),
            json_mode=json_mode,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            finish_reason=finish_reason_name,
            finish_reason_label=_FINISH_REASON_NAMES.get(finish_reason_name, finish_reason_name),
            max_output_tokens=8192,
        )

        if candidate and finish_reason_name not in _OK_FINISH_REASONS:
            logger.warning(
                "gemini_early_termination",
                model=model_name,
                finish_reason=finish_reason_name,
                finish_reason_label=_FINISH_REASON_NAMES.get(finish_reason_name, finish_reason_name),
                output_tokens=output_tokens,
                max_output_tokens=8192,
            )
            raise AIProviderException(
                message=f"Gemini terminated early: finish_reason={finish_reason_name}, "
                        f"output_tokens={output_tokens}/8192"
            )

        text = response.text
        logger.info(
            "gemini_call_ok",
            model=model_name,
            has_images=bool(images),
            image_count=len(images) if images else 0,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            finish_reason=finish_reason_name,
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
        # "Extra data" means Gemini returned valid JSON followed by extra text
        # (e.g. a second JSON block or prose commentary). raw_decode() parses
        # only the first valid object and ignores everything after it.
        if "Extra data" in str(exc):
            try:
                obj, _ = json.JSONDecoder().raw_decode(cleaned)
                logger.warning(
                    "gemini_json_extra_data_recovered",
                    extra_data_pos=exc.pos,
                    raw_text=text[:200],
                )
                return obj
            except json.JSONDecodeError:
                pass

        logger.warning("gemini_json_parse_failed", raw_text=text[:200], error=str(exc))
        raise AIProviderException(
            message=f"Gemini returned invalid JSON: {exc}. Raw: {text[:200]!r}"
        ) from exc
