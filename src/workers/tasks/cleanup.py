"""
workers/tasks/cleanup.py — Periodic DB Cleanup Tasks
======================================================

Runs on a Celery beat schedule to detect and mark orphaned or stuck cases
so they do not accumulate indefinitely in the database.

TWO JOBS
--------
1. mark_orphaned_cases_failed
   Finds cases that are still PENDING with zero images, created more than
   24 hours ago. These are ghost cases left behind when the Flutter app
   crashes or the user force-quits after the backend created the case but
   before any image was uploaded (e.g. blur rejection in the diagnose flow).
   Action: set ai_status=FAILED, is_deleted=True, deleted_at=now.

2. mark_stuck_processing_cases_failed
   Finds cases stuck in PROCESSING for more than 2 hours. These arise when
   a Celery worker crashes mid-task after marking the case PROCESSING but
   before completing the chain. task_acks_late=True handles most crashes,
   but a hard kill (OOM, SIGKILL) can still leave the case stranded.
   Action: set ai_status=FAILED so the doctor or patient can retry.

DB ACCESS
---------
Same pattern as analysis.py — asyncio.run() inside a sync Celery task,
NullPool engine, explicit session commit.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from celery.utils.log import get_task_logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.config import settings
from src.models.case import AiStatus, Case
from src.models.case_image import CaseImage
from src.workers.celery_app import celery_app

logger = get_task_logger(__name__)

# ------------------------------------------------------------------ #
# DB helper
# ------------------------------------------------------------------ #

def _make_session() -> async_sessionmaker:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


# ------------------------------------------------------------------ #
# Task 1 — orphaned cases (PENDING + zero images + older than 24 h)
# ------------------------------------------------------------------ #

@celery_app.task(
    name="cleanup.mark_orphaned_cases_failed",
    max_retries=0,
    ignore_result=True,
)
def mark_orphaned_cases_failed() -> None:
    """Mark PENDING cases with no images older than 24 h as FAILED + soft-deleted."""
    asyncio.run(_mark_orphaned_cases_failed())


async def _mark_orphaned_cases_failed() -> None:
    session_factory = _make_session()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    async with session_factory() as session:
        async with session.begin():
            # Subquery: case IDs that have at least one image
            cases_with_images = select(CaseImage.case_id).distinct().scalar_subquery()

            result = await session.execute(
                select(Case).where(
                    Case.ai_status == AiStatus.PENDING,
                    Case.created_at < cutoff,
                    Case.id.not_in(cases_with_images),
                    Case.is_deleted.is_(False),
                )
            )
            cases = result.scalars().all()

            if not cases:
                logger.info("cleanup_orphaned: no orphaned cases found")
                return

            now = datetime.now(timezone.utc)
            for case in cases:
                case.ai_status = AiStatus.FAILED
                case.is_deleted = True
                case.deleted_at = now

            logger.info(
                "cleanup_orphaned: marked %d orphaned cases as failed+deleted",
                len(cases),
            )


# ------------------------------------------------------------------ #
# Task 2 — stuck PROCESSING cases (older than 2 h)
# ------------------------------------------------------------------ #

@celery_app.task(
    name="cleanup.mark_stuck_processing_cases_failed",
    max_retries=0,
    ignore_result=True,
)
def mark_stuck_processing_cases_failed() -> None:
    """Reset cases stuck in PROCESSING for more than 2 hours to FAILED."""
    asyncio.run(_mark_stuck_processing_cases_failed())


async def _mark_stuck_processing_cases_failed() -> None:
    session_factory = _make_session()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(Case).where(
                    Case.ai_status == AiStatus.PROCESSING,
                    Case.updated_at < cutoff,
                    Case.is_deleted.is_(False),
                )
            )
            cases = result.scalars().all()

            if not cases:
                logger.info("cleanup_stuck: no stuck cases found")
                return

            for case in cases:
                case.ai_status = AiStatus.FAILED

            logger.info(
                "cleanup_stuck: reset %d stuck PROCESSING cases to failed",
                len(cases),
            )
