"""
ai/model_registry.py — Runtime AI Model Name Registry
=======================================================

WHY THIS EXISTS
---------------
`_MODEL_NAME = "gemini-2.0-flash"` was hardcoded in gemini_client.py.
Changing it required editing source code and redeploying.

This module makes model names configurable from the admin panel at runtime:

    PATCH /api/v1/admin/ai-settings
    { "gemini_model": "gemini-1.5-pro" }

The change takes effect immediately — no server restart, no redeployment.

HOW IT WORKS
------------
Model names are stored as key-value pairs in Redis:

    Redis key                        Value
    ─────────────────────────────    ──────────────────
    aiderm:ai:gemini_model      →    gemini-2.0-flash
    aiderm:ai:openai_model      →    gpt-4o
    aiderm:ai:deepseek_model    →    deepseek-chat
    aiderm:ai:default_provider  →    gemini

READ PRIORITY (for each model name):
    1. Redis override  ← set by admin panel
    2. settings.*      ← set in .env
    3. hardcoded default ← last resort

WHY REDIS?
----------
Both the FastAPI API server and Celery workers are separate processes.
In-memory module-level variables only affect the current process.
Redis is already in the stack and is visible to all processes —
API server, Celery workers, and future replicas all read the same values.

GRACEFUL DEGRADATION
---------------------
If Redis is unreachable (connection error), all read functions fall back
to settings.* values silently. The app never crashes because of a Redis
hiccup on a config read.

USAGE
-----
    from src.ai.model_registry import get_gemini_model, set_ai_setting

    model_name = get_gemini_model()          # in gemini_client.py
    set_ai_setting("gemini_model", "gemini-1.5-pro")  # in admin service
"""

import asyncio
from typing import Any

import redis as redis_lib

from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# Redis key prefix — namespaces all AI settings
# ------------------------------------------------------------------ #
_PREFIX = "aiderm:ai:"

# Canonical setting keys
_KEY_GEMINI_MODEL      = "gemini_model"
_KEY_OPENAI_MODEL      = "openai_model"
_KEY_DEEPSEEK_MODEL    = "deepseek_model"
_KEY_DEFAULT_PROVIDER  = "default_provider"

# All valid keys with their .env fallback field names
_VALID_KEYS: dict[str, str] = {
    _KEY_GEMINI_MODEL:     "GEMINI_MODEL",
    _KEY_OPENAI_MODEL:     "OPENAI_MODEL",
    _KEY_DEEPSEEK_MODEL:   "DEEPSEEK_MODEL",
    _KEY_DEFAULT_PROVIDER: "DEFAULT_LLM_PROVIDER",
}


# ------------------------------------------------------------------ #
# Internal Redis helper
# ------------------------------------------------------------------ #

def _get_redis_client() -> redis_lib.Redis:
    """Return a sync Redis client. Called per-operation (not cached)."""
    return redis_lib.from_url(settings.REDIS_URL, decode_responses=True)


def _redis_get(key: str) -> str | None:
    """
    Fetch a value from Redis. Returns None on any error.

    We never let a Redis failure break AI calls.
    """
    try:
        client = _get_redis_client()
        return client.get(f"{_PREFIX}{key}")
    except Exception as exc:
        logger.warning("model_registry_redis_read_failed", key=key, error=str(exc))
        return None


def _redis_set(key: str, value: str) -> None:
    """Write a value to Redis. Raises on failure (admin operation — should surface errors)."""
    client = _get_redis_client()
    client.set(f"{_PREFIX}{key}", value)
    logger.info("model_registry_updated", key=key, value=value)


def _redis_delete(key: str) -> None:
    """Delete a Redis override key, reverting to the .env default."""
    try:
        client = _get_redis_client()
        client.delete(f"{_PREFIX}{key}")
        logger.info("model_registry_reset", key=key)
    except Exception as exc:
        logger.warning("model_registry_redis_delete_failed", key=key, error=str(exc))


# ------------------------------------------------------------------ #
# Public read API  — used by AI clients
# ------------------------------------------------------------------ #

def get_gemini_model() -> str:
    """
    Return the Gemini model name to use for the next API call.

    Priority: Redis override → settings.GEMINI_MODEL → "gemini-2.0-flash"
    """
    return _redis_get(_KEY_GEMINI_MODEL) or settings.GEMINI_MODEL


def get_openai_model() -> str:
    """
    Return the OpenAI model name.

    Priority: Redis override → settings.OPENAI_MODEL → "gpt-4o"
    """
    return _redis_get(_KEY_OPENAI_MODEL) or settings.OPENAI_MODEL


def get_deepseek_model() -> str:
    """
    Return the DeepSeek model name.

    Priority: Redis override → settings.DEEPSEEK_MODEL → "deepseek-chat"
    """
    return _redis_get(_KEY_DEEPSEEK_MODEL) or settings.DEEPSEEK_MODEL


def get_default_provider() -> str:
    """
    Return the default LLM provider.

    Priority: Redis override → settings.DEFAULT_LLM_PROVIDER → "gemini"
    """
    return _redis_get(_KEY_DEFAULT_PROVIDER) or settings.DEFAULT_LLM_PROVIDER


def get_all() -> dict[str, str]:
    """
    Return all effective AI settings (Redis override or .env default).

    Used by GET /admin/ai-settings to show the admin what's currently active.
    """
    return {
        _KEY_GEMINI_MODEL:     get_gemini_model(),
        _KEY_OPENAI_MODEL:     get_openai_model(),
        _KEY_DEEPSEEK_MODEL:   get_deepseek_model(),
        _KEY_DEFAULT_PROVIDER: get_default_provider(),
    }


# ------------------------------------------------------------------ #
# Public write API  — used by admin service
# ------------------------------------------------------------------ #

def set_ai_setting(key: str, value: str) -> None:
    """
    Store an AI setting override in Redis.

    Parameters
    ----------
    key   : One of the canonical keys (e.g. "gemini_model")
    value : The new value (e.g. "gemini-1.5-pro")

    Raises
    ------
    ValueError — if the key is not a valid setting key
    """
    if key not in _VALID_KEYS:
        raise ValueError(
            f"Invalid AI setting key '{key}'. "
            f"Valid keys: {', '.join(_VALID_KEYS)}"
        )
    _redis_set(key, value)


def reset_ai_setting(key: str) -> None:
    """
    Delete a Redis override for the given key, reverting to .env default.

    Parameters
    ----------
    key : One of the canonical keys (e.g. "gemini_model")
    """
    if key not in _VALID_KEYS:
        raise ValueError(
            f"Invalid AI setting key '{key}'. "
            f"Valid keys: {', '.join(_VALID_KEYS)}"
        )
    _redis_delete(key)


# ------------------------------------------------------------------ #
# Async wrappers  — for use inside async FastAPI route handlers
# ------------------------------------------------------------------ #

async def async_get_all() -> dict[str, str]:
    """Async wrapper around get_all() for use in async route handlers."""
    return await asyncio.to_thread(get_all)


async def async_set_ai_setting(key: str, value: str) -> None:
    """Async wrapper around set_ai_setting() for use in async route handlers."""
    await asyncio.to_thread(set_ai_setting, key, value)


async def async_reset_ai_setting(key: str) -> None:
    """Async wrapper around reset_ai_setting() for use in async route handlers."""
    await asyncio.to_thread(reset_ai_setting, key)
