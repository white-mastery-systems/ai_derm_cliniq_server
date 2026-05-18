"""
ai/prompt_registry.py — Runtime AI Prompt Registry
=====================================================

Same pattern as model_registry.py — stores prompt overrides in Redis so
admins can edit prompts from the admin panel without redeploying.

HOW IT WORKS
------------
Each prompt key maps to a Redis value. When a prompt method is called,
it checks Redis first and falls back to the hardcoded default if no
override exists or if Redis is unreachable.

Redis key format:  aiderm:prompt:<key>
Example:           aiderm:prompt:patient_first_question

History key:       aiderm:prompt:history:<key>  (Redis list, newest first)
Each history entry is JSON: {"value": "...", "updated_at": "<ISO datetime>"}
History is capped at _MAX_HISTORY entries per key.

VALID KEYS
----------
See _VALID_KEYS dict below. Each entry maps the key to a human-readable
label for display in the admin panel.

GRACEFUL DEGRADATION
---------------------
If Redis is unreachable, get_prompt() returns None and the prompt
method falls back to the hardcoded default. The app never crashes.
"""

import asyncio
import json
import threading
from datetime import datetime, timezone

import redis as redis_lib

from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)

_PREFIX = "aiderm:prompt:"
_HISTORY_PREFIX = "aiderm:prompt:history:"
_MAX_HISTORY = 10

# Thread-local flag used by get_default_value() to bypass Redis during
# default-text extraction, so _p() falls through to the hardcoded string.
_tl = threading.local()

# All valid prompt keys with their admin-facing labels
_VALID_KEYS: dict[str, str] = {
    # ── Patient flow ──────────────────────────────────────────────────
    "patient_first_question":        "Patient: First Question (image-based)",
    "patient_questions_no_image":    "Patient: Questions Without Image",
    "patient_follow_up_question":    "Patient: Follow-Up Question (rounds 1+)",
    "patient_diagnosis_refinement":  "Patient: Diagnosis Refinement (after answers)",
    "patient_case_summary":          "Patient: Final Case Summary",
    # ── Doctor review flow ────────────────────────────────────────────
    "doctor_complaints":             "Doctor: Technical Complaint Suggestions",
    "doctor_diagnosis":              "Doctor: AI Differential Diagnosis",
    "doctor_question":               "Doctor: Clarifying Question (Q&A)",
    "doctor_treatment_plan":         "Doctor: Treatment Plan Generator",
    "doctor_final_summary":          "Doctor: Final Clinical Summary (PDF)",
}


# ------------------------------------------------------------------ #
# Internal Redis helpers
# ------------------------------------------------------------------ #

def _get_redis_client() -> redis_lib.Redis:
    return redis_lib.from_url(settings.REDIS_URL, decode_responses=True)


def _redis_get(key: str) -> str | None:
    try:
        client = _get_redis_client()
        return client.get(f"{_PREFIX}{key}")
    except Exception as exc:
        logger.warning("prompt_registry_redis_read_failed", key=key, error=str(exc))
        return None


def _redis_set(key: str, value: str) -> None:
    client = _get_redis_client()
    # Save the current value to history before overwriting
    current = client.get(f"{_PREFIX}{key}")
    if current:
        entry = json.dumps({
            "value": current,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        client.lpush(f"{_HISTORY_PREFIX}{key}", entry)
        client.ltrim(f"{_HISTORY_PREFIX}{key}", 0, _MAX_HISTORY - 1)
    client.set(f"{_PREFIX}{key}", value)
    logger.info("prompt_registry_updated", key=key)


def _redis_delete(key: str) -> None:
    try:
        client = _get_redis_client()
        client.delete(f"{_PREFIX}{key}")
        logger.info("prompt_registry_reset", key=key)
    except Exception as exc:
        logger.warning("prompt_registry_redis_delete_failed", key=key, error=str(exc))


def _redis_get_history(key: str) -> list[dict]:
    try:
        client = _get_redis_client()
        raw = client.lrange(f"{_HISTORY_PREFIX}{key}", 0, _MAX_HISTORY - 1)
        return [json.loads(entry) for entry in raw]
    except Exception as exc:
        logger.warning("prompt_registry_history_read_failed", key=key, error=str(exc))
        return []


def _redis_rollback(key: str) -> str | None:
    """
    Pop the most recent history entry and set it as the current value.
    Returns the restored value, or None if history is empty.
    """
    client = _get_redis_client()
    raw = client.lpop(f"{_HISTORY_PREFIX}{key}")
    if raw is None:
        return None
    entry = json.loads(raw)
    previous_value = entry["value"]
    # Set directly (do NOT push to history — this is an undo, not a new edit)
    client.set(f"{_PREFIX}{key}", previous_value)
    logger.info("prompt_registry_rolled_back", key=key)
    return previous_value


# ------------------------------------------------------------------ #
# Public read API — used by prompt classes
# ------------------------------------------------------------------ #

def get_prompt(key: str) -> str | None:
    """
    Return the Redis override for this prompt key, or None if not set.
    Returns None unconditionally when called from get_default_value() so that
    _p() falls through to its hardcoded string.

    Prompt methods call this and fall back to their hardcoded default
    when None is returned.
    """
    if key not in _VALID_KEYS:
        return None
    if getattr(_tl, "bypass_redis", False):
        return None
    return _redis_get(key)


def _get_prompt_getters() -> dict[str, callable]:
    """Lazy-import mapping of key → prompt method callable."""
    from src.ai.prompts.image_analysis_prompts import ImageAnalysisPrompts
    from src.ai.prompts.patient_consultation_prompts import PatientConsultationPrompts
    from src.ai.prompts.doctor_review_prompts import DoctorReviewPrompts
    return {
        "patient_first_question":       ImageAnalysisPrompts.first_question,
        "patient_questions_no_image":   PatientConsultationPrompts.generate_questions_from_complaints,
        "patient_follow_up_question":   PatientConsultationPrompts.generate_question_from_context,
        "patient_diagnosis_refinement": PatientConsultationPrompts.diagnosis_analysis_from_conversation,
        "patient_case_summary":         PatientConsultationPrompts.make_case_summary,
        "doctor_complaints":            DoctorReviewPrompts.get_technical_complaints,
        "doctor_diagnosis":             DoctorReviewPrompts.generate_diagnosis,
        "doctor_question":              DoctorReviewPrompts.generate_doctor_question_direct,
        "doctor_final_summary":         DoctorReviewPrompts.generate_final_summary,
        "doctor_treatment_plan":        DoctorReviewPrompts.generate_treatment_plan,
    }


def get_default_value(key: str) -> str | None:
    """
    Return the hardcoded default prompt text for a key, bypassing Redis.

    Temporarily sets a thread-local flag so get_prompt() returns None,
    causing _p() in each prompt method to fall through to its hardcoded string.
    Thread-safe — concurrent requests are unaffected.
    """
    getters = _get_prompt_getters()
    getter = getters.get(key)
    if getter is None:
        return None
    _tl.bypass_redis = True
    try:
        return getter()
    except Exception as exc:
        logger.warning("prompt_default_fetch_failed", key=key, error=str(exc))
        return None
    finally:
        _tl.bypass_redis = False


def get_all() -> dict[str, dict]:
    """
    Return all prompt keys with their current values and metadata.

    Used by GET /admin/prompts.
    Returns a dict keyed by prompt_key with:
        { "label": str, "value": str | None, "default_value": str | None, "has_override": bool }
    """
    result = {}
    for key, label in _VALID_KEYS.items():
        override = _redis_get(key)
        result[key] = {
            "key": key,
            "label": label,
            "value": override,
            "default_value": get_default_value(key),
            "has_override": override is not None,
        }
    return result


def get_history(key: str) -> list[dict]:
    """Return the version history for a prompt key (newest first)."""
    if key not in _VALID_KEYS:
        raise ValueError(f"Invalid prompt key '{key}'. Valid keys: {', '.join(_VALID_KEYS)}")
    return _redis_get_history(key)


# ------------------------------------------------------------------ #
# Public write API — used by admin service
# ------------------------------------------------------------------ #

def set_prompt(key: str, value: str) -> None:
    if key not in _VALID_KEYS:
        raise ValueError(f"Invalid prompt key '{key}'. Valid keys: {', '.join(_VALID_KEYS)}")
    _redis_set(key, value)


def reset_prompt(key: str) -> None:
    if key not in _VALID_KEYS:
        raise ValueError(f"Invalid prompt key '{key}'. Valid keys: {', '.join(_VALID_KEYS)}")
    _redis_delete(key)


def rollback_prompt(key: str) -> str | None:
    """
    Restore the previous version of a prompt from history.

    Returns the restored value, or None if no history exists for this key.
    Raises ValueError if the key is invalid.
    """
    if key not in _VALID_KEYS:
        raise ValueError(f"Invalid prompt key '{key}'. Valid keys: {', '.join(_VALID_KEYS)}")
    return _redis_rollback(key)


# ------------------------------------------------------------------ #
# Async wrappers — for use inside async FastAPI route handlers
# ------------------------------------------------------------------ #

async def async_get_all() -> dict[str, dict]:
    return await asyncio.to_thread(get_all)


async def async_set_prompt(key: str, value: str) -> None:
    await asyncio.to_thread(set_prompt, key, value)


async def async_reset_prompt(key: str) -> None:
    await asyncio.to_thread(reset_prompt, key)


async def async_get_history(key: str) -> list[dict]:
    return await asyncio.to_thread(get_history, key)


async def async_rollback_prompt(key: str) -> str | None:
    return await asyncio.to_thread(rollback_prompt, key)
