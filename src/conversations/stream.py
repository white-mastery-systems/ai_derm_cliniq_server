"""
conversations/stream.py — Server-Sent Events (SSE) Chat Stream
===============================================================

Implements GET /api/v1/cases/{case_id}/chat/stream

The Flutter app opens this endpoint and keeps the connection open.
The server pushes events as the AI processes the case and generates
questions, instead of the client having to poll GET /chat repeatedly.

WHY SSE INSTEAD OF WEBSOCKET?
-------------------------------
- SSE is unidirectional (server → client only) — exactly what we need.
  The patient sends answers via the existing POST /answers endpoint,
  not via this stream.
- SSE is HTTP/1.1 compatible, works through proxies, no upgrade handshake.
- WebSockets need more infra (connection management, reconnection) — SSE
  reconnects automatically via the browser/Flutter http client.
- Simpler to implement, test, and reason about.

EVENT TYPES
-----------
The stream emits these named events (Flutter listens by event name):

  status_update   — case ai_status changed (pending → processing → completed)
                    data: {"ai_status": "processing", "case_id": "..."}

  questions_ready — New AI questions appeared for the current round
                    data: {"round": 0, "questions": [...], "case_id": "..."}

  round_complete  — Patient answers were recorded, next round starting
                    data: {"round": 1, "case_id": "..."}

  conversation_complete — max_question_rounds reached, case summary ready
                    data: {"case_id": "...", "case_summary": "..."}

  ping            — Heartbeat every HEARTBEAT_INTERVAL seconds.
                    Keeps the connection alive through proxies/load balancers.
                    data: {"t": <unix_timestamp>}

  error           — Fatal error; client should close connection.
                    data: {"message": "..."}

POLLING INTERVAL
-----------------
The stream polls the DB every POLL_INTERVAL_SECONDS (1 second).
This is acceptable for a medical app — real-time latency isn't critical.
The Celery tasks typically take 5–30 seconds, so a 1s poll is responsive.

MAX DURATION
------------
The stream auto-closes after MAX_STREAM_SECONDS (5 minutes).
Clients must reconnect after this. This prevents zombie connections
from accumulating in production.

AUTHENTICATION
--------------
The patient's JWT is passed as a Bearer token in the Authorization header
(standard for all endpoints). The stream checks access on first connection.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.exceptions import CaseNotFoundException, ForbiddenException
from src.logger import get_logger
from src.models.case import AiStatus, Case
from src.models.message import Message, MessageRole
from src.models.user import User, UserRole

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# Configuration
# ------------------------------------------------------------------ #
POLL_INTERVAL_SECONDS: float = 1.0
HEARTBEAT_INTERVAL: int = 15          # emit ping every N seconds
MAX_STREAM_SECONDS: int = 300         # auto-close after 5 minutes


# ------------------------------------------------------------------ #
# SSE helpers
# ------------------------------------------------------------------ #

def _sse(event: str, data: dict) -> str:
    """Format one SSE message with named event and JSON data."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _ping() -> str:
    return f"event: ping\ndata: {json.dumps({'t': int(time.time())})}\n\n"


# ------------------------------------------------------------------ #
# Access check (called once on stream open)
# ------------------------------------------------------------------ #

async def _check_access(db: AsyncSession, case_id: str, user: User) -> Case:
    """
    Verify the case exists and the user has access.
    Raises CaseNotFoundException or ForbiddenException.
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()

    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if user.role == UserRole.PATIENT and case.patient_id != user.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
        raise ForbiddenException(message="You are not assigned to this case")

    return case


# ------------------------------------------------------------------ #
# Message snapshot helpers
# ------------------------------------------------------------------ #

async def _get_ai_message_ids(db: AsyncSession, case_id: str) -> set[str]:
    """Return IDs of all AI messages for the case (for change detection)."""
    result = await db.execute(
        select(Message.id)
        .where(Message.case_id == case_id, Message.role == MessageRole.AI)
    )
    return {row[0] for row in result}


async def _get_current_questions(
    db: AsyncSession, case_id: str, round_number: int
) -> list[dict]:
    """
    Return parsed questions for a given round.
    Returns empty list if questions haven't appeared yet.
    """
    result = await db.execute(
        select(Message)
        .where(
            Message.case_id == case_id,
            Message.round_number == round_number,
            Message.role == MessageRole.AI,
        )
        .order_by(Message.question_index)
    )
    msgs = list(result.scalars().all())

    questions = []
    for msg in msgs:
        try:
            data = json.loads(msg.content)
            questions.append({
                "question": data.get("question", ""),
                "answer_options": data.get("answer_options", []),
                "question_index": msg.question_index,
            })
        except (json.JSONDecodeError, TypeError):
            pass
    return questions


# ------------------------------------------------------------------ #
# Main generator
# ------------------------------------------------------------------ #

async def chat_stream_generator(
    db: AsyncSession,
    case_id: str,
    user: User,
) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted strings.

    Consumed by FastAPI's StreamingResponse.
    Auto-closes after MAX_STREAM_SECONDS or when conversation is complete.

    Usage in controller:
        return StreamingResponse(
            chat_stream_generator(db, case_id, user),
            media_type="text/event-stream",
        )
    """
    # ── Initial access check ─────────────────────────────────────────── #
    try:
        case = await _check_access(db, case_id, user)
    except (CaseNotFoundException, ForbiddenException) as exc:
        yield _sse("error", {"message": str(exc)})
        return

    # ── State tracking (detect changes across polls) ─────────────────── #
    last_ai_status: AiStatus = case.ai_status
    last_question_round: int = case.question_round
    last_ai_msg_ids: set[str] = await _get_ai_message_ids(db, case_id)

    # Emit initial state on connection
    yield _sse("status_update", {
        "case_id": case_id,
        "ai_status": case.ai_status.value,
        "question_round": case.question_round,
        "max_rounds": case.max_question_rounds,
    })

    # If questions already exist for the current round, emit them immediately
    if last_ai_msg_ids:
        questions = await _get_current_questions(db, case_id, case.question_round)
        if questions:
            yield _sse("questions_ready", {
                "case_id": case_id,
                "round": case.question_round,
                "questions": questions,
            })

    start_time = time.monotonic()
    last_ping_time = start_time

    # ── Poll loop ─────────────────────────────────────────────────────── #
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        now = time.monotonic()

        # Auto-close after max duration
        if now - start_time > MAX_STREAM_SECONDS:
            yield _sse("error", {
                "message": "Stream timeout. Please reconnect.",
                "case_id": case_id,
            })
            return

        # Heartbeat
        if now - last_ping_time >= HEARTBEAT_INTERVAL:
            yield _ping()
            last_ping_time = now

        # Re-fetch case from DB
        result = await db.execute(select(Case).where(Case.id == case_id))
        case = result.scalar_one_or_none()
        if case is None:
            yield _sse("error", {"message": "Case no longer exists."})
            return

        # ── Detect ai_status change ──────────────────────────────────── #
        if case.ai_status != last_ai_status:
            last_ai_status = case.ai_status
            yield _sse("status_update", {
                "case_id": case_id,
                "ai_status": case.ai_status.value,
                "question_round": case.question_round,
                "case_summary": case.case_summary,
            })

        # ── Detect new AI messages (questions appeared) ──────────────── #
        current_ai_msg_ids = await _get_ai_message_ids(db, case_id)
        new_ids = current_ai_msg_ids - last_ai_msg_ids

        if new_ids:
            last_ai_msg_ids = current_ai_msg_ids
            questions = await _get_current_questions(db, case_id, case.question_round)
            if questions:
                yield _sse("questions_ready", {
                    "case_id": case_id,
                    "round": case.question_round,
                    "questions": questions,
                })

        # ── Detect round advancement ─────────────────────────────────── #
        if case.question_round != last_question_round:
            last_question_round = case.question_round
            is_complete = case.question_round >= case.max_question_rounds

            if is_complete:
                yield _sse("conversation_complete", {
                    "case_id": case_id,
                    "case_summary": case.case_summary,
                    "question_round": case.question_round,
                })
                return   # Close the stream — conversation is done

            yield _sse("round_complete", {
                "case_id": case_id,
                "round": case.question_round,
            })
