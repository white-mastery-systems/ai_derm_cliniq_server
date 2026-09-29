"""
models/prompt_override.py — Persistent AI Prompt Overrides
===========================================================

WHY THIS TABLE EXISTS
---------------------
Admin prompt overrides were previously stored ONLY in Redis.
Redis data lives in memory and relies on periodic RDB snapshots.
If the Redis container is restarted between snapshots, or if the
Docker volume is wiped, all prompt customisations are silently lost.

These two tables make PostgreSQL the source of truth:
  - PromptOverride  : the *current* active override for a key (upsert on write)
  - PromptHistory   : the last 10 replaced values per key (for rollback)

Redis is kept as a fast-read cache. On every server startup the lifespan
calls warm_redis_from_db() to reload all rows into Redis, so the read
path for Celery workers (which query Redis) is never affected.

WRITE FLOW
----------
  Admin PATCH /admin/prompts/{key}
    → save old value to PromptHistory (PostgreSQL)
    → upsert new value into PromptOverride (PostgreSQL)
    → update Redis key (cache refresh)

READ FLOW (unchanged for workers)
----------
  Celery task calls get_prompt(key)
    → Redis hit  → return value
    → Redis miss → return None → prompt method uses hardcoded default

ROLLBACK FLOW
-------------
  Admin POST /admin/prompts/{key}/rollback
    → read latest PromptHistory row
    → delete that history row
    → upsert the restored value into PromptOverride
    → update Redis key
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, new_uuid


class PromptOverride(Base):
    """
    One row per prompt key — the currently active custom override.

    Rows are upserted on PATCH /admin/prompts/{key} and deleted on
    DELETE /admin/prompts/{key}. An absent row means the prompt uses
    its hardcoded default.
    """

    __tablename__ = "prompt_overrides"

    key: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
        comment="Prompt key (e.g. 'patient_follow_up_question')",
    )
    value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The custom prompt text set by the admin",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="When this override was last written",
    )

    def __repr__(self) -> str:
        return f"<PromptOverride key={self.key!r} len={len(self.value)}>"


class PromptHistory(Base):
    """
    Append-only log of replaced prompt values (newest first, max 10 per key).

    When an override is updated, the previous value is pushed here.
    Rollback pops the most recent row and restores it as the current override.
    Rows beyond position 10 are pruned after each write.
    """

    __tablename__ = "prompt_history"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_uuid,
    )
    key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Prompt key this history entry belongs to",
    )
    value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The prompt text that was active before the next write",
    )
    replaced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="When this value was replaced (used for ordering)",
    )

    def __repr__(self) -> str:
        return (
            f"<PromptHistory key={self.key!r} "
            f"replaced_at={self.replaced_at}>"
        )
