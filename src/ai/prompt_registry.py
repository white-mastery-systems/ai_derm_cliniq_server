"""
ai/prompt_registry.py — Runtime AI Prompt Registry
=====================================================

Same pattern as model_registry.py — stores prompt overrides in Redis so
admins can edit prompts from the admin panel without redeploying.

HOW IT WORKS
------------
PostgreSQL is the SOURCE OF TRUTH. Redis is a fast-read cache.

  Write path  : admin PATCH → save history to PostgreSQL → upsert to
                PostgreSQL → update Redis cache. Atomic — DB first.

  Read path   : Celery workers call get_prompt() → Redis hit (fast).
                Redis is always warm because warm_redis_from_db() runs
                at server startup and after every write.

  History     : stored in PostgreSQL (PromptHistory table), not Redis
                lists. Max 10 entries per key, trimmed on each write.

  Rollback    : pops latest PromptHistory row → restores it to
                PromptOverride and Redis.

Redis key format:  aiderm:prompt:<key>
Example:           aiderm:prompt:patient_first_question

GRACEFUL DEGRADATION
---------------------
If Redis is unreachable, get_prompt() returns None and the prompt
method falls back to the hardcoded default. The app never crashes.
Writes still succeed (PostgreSQL committed first); Redis is updated
best-effort.
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


# ------------------------------------------------------------------ #
# PostgreSQL-backed write API (used by admin service)
# ------------------------------------------------------------------ #
# These functions accept an AsyncSession so writes are committed to
# PostgreSQL first, then Redis is updated as a cache refresh.
# History lives in PromptHistory rows (not Redis lists).

_MAX_DB_HISTORY = 10


async def db_set_prompt(db, key: str, value: str) -> None:
    """
    Upsert a prompt override to PostgreSQL (primary) and Redis (cache).

    If a current override exists, the old value is saved to PromptHistory
    before the upsert. History is trimmed to _MAX_DB_HISTORY entries.

    Raises ValueError if the key is not in _VALID_KEYS.
    """
    from datetime import datetime, timezone

    from sqlalchemy import delete, select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from src.models.prompt_override import PromptHistory, PromptOverride

    if key not in _VALID_KEYS:
        raise ValueError(f"Invalid prompt key '{key}'. Valid keys: {', '.join(_VALID_KEYS)}")

    # 1. Read current value so we can save it to history
    result = await db.execute(
        select(PromptOverride).where(PromptOverride.key == key)
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        history_entry = PromptHistory(
            key=key,
            value=existing.value,
            replaced_at=datetime.now(timezone.utc),
        )
        db.add(history_entry)
        await db.flush()

        # Trim history to keep only the latest _MAX_DB_HISTORY entries
        count_result = await db.execute(
            select(PromptHistory).where(PromptHistory.key == key)
            .order_by(PromptHistory.replaced_at.desc())
        )
        all_history = count_result.scalars().all()
        if len(all_history) > _MAX_DB_HISTORY:
            oldest_ids = [row.id for row in all_history[_MAX_DB_HISTORY:]]
            await db.execute(
                delete(PromptHistory).where(PromptHistory.id.in_(oldest_ids))
            )

    # 2. Upsert the new value into PromptOverride
    if existing is not None:
        existing.value = value
    else:
        db.add(PromptOverride(key=key, value=value))

    await db.commit()

    # 3. Update Redis cache (best-effort)
    try:
        _redis_set(key, value)
    except Exception as exc:
        logger.warning("prompt_redis_cache_update_failed", key=key, error=str(exc))

    logger.info("prompt_db_updated", key=key)


async def db_reset_prompt(db, key: str) -> None:
    """
    Delete a prompt override from PostgreSQL and remove the Redis cache key.

    History rows are preserved so admins can still see what was set.
    Raises ValueError if the key is not in _VALID_KEYS.
    """
    from sqlalchemy import delete

    from src.models.prompt_override import PromptOverride

    if key not in _VALID_KEYS:
        raise ValueError(f"Invalid prompt key '{key}'. Valid keys: {', '.join(_VALID_KEYS)}")

    await db.execute(delete(PromptOverride).where(PromptOverride.key == key))
    await db.commit()

    # Clear Redis cache (best-effort)
    try:
        _redis_delete(key)
    except Exception as exc:
        logger.warning("prompt_redis_cache_delete_failed", key=key, error=str(exc))

    logger.info("prompt_db_reset", key=key)


async def db_get_history(db, key: str) -> list[dict]:
    """
    Return the version history for a prompt key from PostgreSQL, newest first.

    Raises ValueError if the key is not in _VALID_KEYS.
    """
    from sqlalchemy import select

    from src.models.prompt_override import PromptHistory

    if key not in _VALID_KEYS:
        raise ValueError(f"Invalid prompt key '{key}'. Valid keys: {', '.join(_VALID_KEYS)}")

    result = await db.execute(
        select(PromptHistory)
        .where(PromptHistory.key == key)
        .order_by(PromptHistory.replaced_at.desc())
        .limit(_MAX_DB_HISTORY)
    )
    rows = result.scalars().all()
    return [
        {"value": row.value, "updated_at": row.replaced_at.isoformat()}
        for row in rows
    ]


async def db_rollback_prompt(db, key: str) -> str | None:
    """
    Restore the most recent previous version of a prompt.

    Pops the latest PromptHistory row, upserts it into PromptOverride,
    and refreshes the Redis cache.

    Returns the restored value, or None if no history exists.
    Raises ValueError if the key is not in _VALID_KEYS.
    """
    from sqlalchemy import delete, select

    from src.models.prompt_override import PromptHistory, PromptOverride

    if key not in _VALID_KEYS:
        raise ValueError(f"Invalid prompt key '{key}'. Valid keys: {', '.join(_VALID_KEYS)}")

    result = await db.execute(
        select(PromptHistory)
        .where(PromptHistory.key == key)
        .order_by(PromptHistory.replaced_at.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is None:
        return None

    restored_value = latest.value
    history_id = latest.id

    # Delete this history entry (it's now the active value again)
    await db.execute(delete(PromptHistory).where(PromptHistory.id == history_id))

    # Upsert the restored value as the current override
    existing_result = await db.execute(
        select(PromptOverride).where(PromptOverride.key == key)
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        existing.value = restored_value
    else:
        db.add(PromptOverride(key=key, value=restored_value))

    await db.commit()

    # Refresh Redis cache (best-effort)
    try:
        client = _get_redis_client()
        client.set(f"{_PREFIX}{key}", restored_value)
    except Exception as exc:
        logger.warning("prompt_redis_rollback_cache_failed", key=key, error=str(exc))

    logger.info("prompt_db_rolled_back", key=key)
    return restored_value


async def warm_redis_from_db(db) -> None:
    """
    Load all active prompt overrides from PostgreSQL into Redis.

    Called once during server startup (lifespan) so the Redis cache is
    always warm before the first request. This means Celery workers that
    read from Redis will always see the latest overrides even after a
    Redis restart.
    """
    from sqlalchemy import select

    from src.models.prompt_override import PromptOverride

    try:
        result = await db.execute(select(PromptOverride))
        rows = result.scalars().all()
        if not rows:
            logger.info("prompt_cache_warm_skipped", reason="no overrides in database")
            return
        client = _get_redis_client()
        for row in rows:
            client.set(f"{_PREFIX}{row.key}", row.value)
        logger.info("prompt_cache_warmed", count=len(rows))
    except Exception as exc:
        # Never block startup — log and continue
        logger.warning("prompt_cache_warm_failed", error=str(exc))
