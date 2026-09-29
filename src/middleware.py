"""
middleware.py — Custom Request Middleware
==========================================

Two middleware classes are defined here:

1. RequestIDMiddleware
   -------------------
   Generates a unique ID (UUID4) for every incoming request.

   WHY?
   Every request gets tagged with a unique ID that appears in every log line
   produced during that request's lifetime. When something goes wrong, you
   can grep all logs for that one ID and see the full story — what was called,
   what the DB returned, what the AI said, what error was raised.

   Without a request ID, logs from concurrent requests get interleaved and
   are nearly impossible to trace.

   The ID is:
   - Bound to structlog's context → appears in ALL log lines for this request
   - Added to the response header X-Request-ID → Flutter can read it and
     include it in bug reports

2. TimingMiddleware
   ----------------
   Measures how long every request takes and logs it.

   WHY?
   You need to know:
   - Which endpoints are slow (candidates for optimisation)
   - When a fast endpoint suddenly becomes slow (regression detection)
   - How long AI calls add to the overall response time

   Every completed request emits one structured log line:
       {
         "event": "request_finished",
         "method": "POST",
         "path": "/api/v1/cases/abc/ai/analyze",
         "status_code": 202,
         "duration_ms": 47.3,
         "request_id": "f3a1b2c4-...",
         "slow": false
       }

   The "slow" flag is True when duration_ms > SLOW_REQUEST_THRESHOLD_MS (500ms).
   In production, you can alert on slow=true log lines.

   Response header X-Process-Time: 47.31ms is also set, so Flutter devs
   can measure API response times from the client side.

MIDDLEWARE ORDER IN FASTAPI
-----------------------------
Middleware is applied in REVERSE registration order to requests.
The LAST middleware added is the OUTERMOST wrapper (first to see the request).

In main.py we register:
    app.add_middleware(CORSMiddleware)       ← innermost (closest to route)
    app.add_middleware(SlowAPIMiddleware)    ← rate limiting
    app.add_middleware(TimingMiddleware)     ← times the whole request
    app.add_middleware(RequestIDMiddleware)  ← outermost (first to run)

So the execution order for a request is:
    RequestIDMiddleware → TimingMiddleware → SlowAPIMiddleware → CORSMiddleware → Route

This means:
- RequestID is set BEFORE timing starts (so it appears in timing logs)
- Timing wraps everything INSIDE it, including rate-limit checks
"""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from src.logger import get_logger

logger = get_logger(__name__)

# Requests slower than this threshold are flagged as slow in logs.
# 500ms is a reasonable baseline for a production API with DB + AI calls.
# AI analysis endpoints are excluded from "slow" classification because
# they always take seconds — they enqueue to Celery and return 202 fast.
SLOW_REQUEST_THRESHOLD_MS: float = 500.0

# Paths to skip detailed timing logs (noise reduction)
# Health checks run every 30s — logging each one is wasteful.
_SKIP_LOG_PATHS: set[str] = {"/health", "/favicon.ico"}


# ------------------------------------------------------------------ #
# Middleware 1 — Request ID
# ------------------------------------------------------------------ #

class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Assign a unique request_id to every incoming HTTP request.

    The ID is:
    1. Bound into structlog's contextvars → appears in ALL log lines
       produced anywhere during this request (services, workers, etc.)
    2. Added to the response as X-Request-ID header
    3. Honoured from the client if X-Request-ID is already in the
       incoming request headers (useful for distributed tracing where
       the Flutter app generates the ID and passes it in)
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        # Accept an ID from the client (distributed tracing), or generate one
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        # Bind request_id into structlog's per-request context.
        # Any logger.info(...) call within this request will automatically
        # include request_id= in the log output.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # Store on request.state so other middleware/routes can access it
        request.state.request_id = request_id

        # Pre-initialise slowapi's state attribute.
        # SlowAPIMiddleware injects rate-limit headers using request.state.view_rate_limit.
        # If a route has no @limiter.limit() decorator (e.g. /health), or if the
        # Redis storage call fails and is swallowed, the attribute is never set —
        # slowapi then crashes with AttributeError. Setting it to None here
        # prevents that crash; slowapi skips header injection when value is None.
        request.state.view_rate_limit = None

        response = await call_next(request)

        # Echo the ID back so the client can reference it in bug reports
        response.headers["X-Request-ID"] = request_id

        return response


# ------------------------------------------------------------------ #
# Middleware 2 — Request Timing
# ------------------------------------------------------------------ #

class TimingMiddleware(BaseHTTPMiddleware):
    """
    Measure and log the wall-clock duration of every HTTP request.

    Emits one structured log line per request:
        event="request_finished"
        method="POST"
        path="/api/v1/cases/abc123/ai/analyze"
        status_code=202
        duration_ms=47.3
        slow=false

    Also sets the X-Process-Time response header for client-side measurement.

    EXCEPTION HANDLING
    ------------------
    If the route raises an unhandled exception, we still log the timing
    and mark status_code=500. The exception is then re-raised so FastAPI's
    exception handlers can produce the proper error response.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        status_code = 500  # default — overwritten on success

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response

        except Exception:
            # Let FastAPI exception handlers deal with it
            raise

        finally:
            # This block runs whether the request succeeded or raised
            duration_ms = (time.perf_counter() - start) * 1000
            path = request.url.path
            method = request.method
            is_slow = duration_ms > SLOW_REQUEST_THRESHOLD_MS

            if path not in _SKIP_LOG_PATHS:
                log_fn = logger.warning if is_slow else logger.info
                log_fn(
                    "request_finished",
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_ms=round(duration_ms, 2),
                    slow=is_slow,
                )

            # Set timing header on successful responses
            # (response may not exist if an exception was raised before call_next)
            try:
                response.headers["X-Process-Time"] = f"{duration_ms:.2f}ms"
            except Exception:
                pass  # Response object may not exist on unhandled exceptions
