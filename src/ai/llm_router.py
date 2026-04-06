"""
ai/llm_router.py — Multi-Provider LLM Router with Automatic Fallback
=====================================================================

Single entry point for ALL LLM calls in this application.

Instead of calling gemini_client.call_gemini() directly, all tasks call:

    from src.ai.llm_router import call_llm, extract_json

The router tries providers in priority order and falls back automatically
if a provider fails (rate-limited, quota exceeded, key missing, etc.).

PROVIDER PRIORITY (configured in .env via DEFAULT_LLM_PROVIDER)
----------------------------------------------------------------
If DEFAULT_LLM_PROVIDER=gemini (default):
    1. Gemini 2.0 Flash    (primary — fast, vision-capable, cost-efficient)
    2. OpenAI GPT-4o       (fallback 1 — strong vision support)
    3. DeepSeek Chat       (fallback 2 — text-only, cheap)

If DEFAULT_LLM_PROVIDER=openai:
    1. OpenAI GPT-4o
    2. Gemini 2.0 Flash
    3. DeepSeek Chat

If DEFAULT_LLM_PROVIDER=deepseek:
    1. DeepSeek Chat
    2. Gemini 2.0 Flash
    3. OpenAI GPT-4o

WHY FALLBACK?
-------------
AI providers have transient failures:
- Rate limit exceeded (429)
- Quota exceeded for the day
- Regional outages
- Model overload

Without fallback, any provider outage means the entire analysis pipeline
fails. With fallback, one provider outage is transparent to users.

HOW IT WORKS
------------
Each provider call is wrapped in try/except AIProviderException.
If the call raises, we log the failure and try the next provider.
If ALL providers fail, we raise AIProviderException with a summary
of all failures — the Celery task catches this and marks the case FAILED.

IMAGES
------
Image bytes are passed to vision-capable providers (Gemini, OpenAI GPT-4o).
DeepSeek (text-only) receives a note in the prompt that images were present.
This means image analysis falls back to text-based reasoning if Gemini/OpenAI
are both down — degraded but not broken.

LOGGING
-------
Every provider attempt is logged with success/failure and which provider
ultimately responded. This makes debugging fallback incidents easy.

EXTRACT_JSON
------------
Re-exported from gemini_client (the canonical implementation) so callers
only need to import from llm_router and not from individual clients.
"""

from src.ai import gemini_client, openai_client
from src.config import settings
from src.exceptions import AIProviderException
from src.logger import get_logger

logger = get_logger(__name__)

# Re-export extract_json — callers use `from src.ai.llm_router import extract_json`
extract_json = gemini_client.extract_json


# Default fallback order if the primary provider fails
_FALLBACK_ORDER: dict[str, list[str]] = {
    "gemini":   ["gemini",   "openai",  "deepseek"],
    "openai":   ["openai",   "gemini",  "deepseek"],
    "deepseek": ["deepseek", "gemini",  "openai"],
}


def _get_provider_order() -> list[tuple[str, str, callable]]:
    """
    Return the ordered list of (key, display_name, call_fn) to try.

    Reads DEFAULT_LLM_PROVIDER from settings (defaults to "gemini").
    Unknown values fall back to the gemini order.

    NOTE: The registry is built here (not at module level) so that
    each call does a live attribute lookup on the client modules.
    This allows tests to patch gemini_client.call_gemini /
    openai_client.call_openai without the references being frozen
    at import time inside a module-level dict.
    """
    # Live lookups — patching gemini_client.call_gemini replaces the
    # attribute on the module object, which is what we access here.
    registry: dict[str, tuple[str, callable]] = {
        "gemini":   ("Gemini 2.0 Flash", gemini_client.call_gemini),
        "openai":   ("GPT-4o",           openai_client.call_openai),
        "deepseek": ("DeepSeek Chat",    openai_client.call_deepseek),
    }

    primary = settings.DEFAULT_LLM_PROVIDER.lower()
    order = _FALLBACK_ORDER.get(primary, _FALLBACK_ORDER["gemini"])

    return [
        (key, *registry[key])
        for key in order
        if key in registry
    ]


# ------------------------------------------------------------------ #
# Public interface
# ------------------------------------------------------------------ #

def call_llm(prompt: str, images: list[bytes] | None = None) -> str:
    """
    Send a prompt (+ optional images) to the best available LLM provider.

    Tries providers in priority order (set by DEFAULT_LLM_PROVIDER in .env).
    Falls back to the next provider if the current one raises AIProviderException.

    Parameters
    ----------
    prompt : str            — Text instruction for the LLM
    images : list[bytes]    — Optional raw image bytes (JPEG/PNG)

    Returns
    -------
    str — Raw LLM response text (may contain JSON, markdown, etc.)

    Raises
    ------
    AIProviderException — all configured providers failed
    """
    providers = _get_provider_order()
    failures: list[str] = []

    for key, display_name, call_fn in providers:
        try:
            result = call_fn(prompt, images)
            if failures:
                # Log that we fell back — useful for debugging
                logger.warning(
                    "llm_router_fallback_succeeded",
                    provider=key,
                    display_name=display_name,
                    failed_providers=failures,
                )
            else:
                logger.info("llm_router_primary_ok", provider=key)
            return result

        except AIProviderException as exc:
            logger.warning(
                "llm_router_provider_failed",
                provider=key,
                display_name=display_name,
                error=str(exc),
            )
            failures.append(f"{display_name}: {exc}")

    # All providers failed
    failure_summary = " | ".join(failures)
    logger.error("llm_router_all_providers_failed", failures=failure_summary)
    raise AIProviderException(
        message=f"All LLM providers failed. Details: {failure_summary}"
    )
