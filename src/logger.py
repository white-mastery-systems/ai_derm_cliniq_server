"""
logger.py — Structured Logging Setup
======================================

WHY STRUCTURED LOGGING?
------------------------
Traditional logging:
    "INFO - User 12345 logged in from 192.168.1.1"

Structured logging (JSON):
    {"level": "info", "event": "user_login", "user_id": "12345",
     "ip": "192.168.1.1", "timestamp": "2026-03-31T10:00:00Z"}

The JSON format lets you:
- Filter logs by field: "show me all events for user_id=12345"
- Alert on patterns: "alert when error_code=DB_CONNECTION_FAILED"
- Aggregate metrics from logs

We use `structlog` which wraps Python's standard `logging` module
and adds context binding, processors, and JSON rendering.

HOW TO USE IN YOUR CODE
------------------------
    from src.logger import get_logger

    logger = get_logger(__name__)

    # Simple message
    logger.info("case_created", case_id=str(case.id), patient_id=str(user.id))

    # With bound context (all subsequent log calls include these fields)
    log = logger.bind(request_id="abc123", user_id="xyz")
    log.info("processing_started")
    log.warning("slow_query", duration_ms=450)
    log.error("ai_failed", provider="gemini", error=str(e))

PROCESSORS PIPELINE
--------------------
Each log record passes through a chain of processors before rendering:
1. add_log_level       → adds "level": "info"
2. add_timestamp       → adds "timestamp": "2026-..."
3. StackInfoRenderer   → adds stack info for exceptions
4. format_exc_info     → formats Python exception tracebacks
5. UnicodeDecoder      → ensures all strings are unicode
6. JSONRenderer        → converts the dict to a JSON string
"""

import logging
import sys

import structlog

from src.config import settings


def setup_logging() -> None:
    """
    Configure structlog and Python's standard logging.
    Call this ONCE at application startup (in main.py lifespan).
    """

    # Choose renderer based on environment:
    # - Development: ConsoleRenderer gives colourful, human-readable output.
    # - Production: JSONRenderer outputs one JSON object per line.
    if settings.is_development:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            # Merge context from structlog.contextvars (request-scoped context)
            structlog.contextvars.merge_contextvars,
            # Add log level name ("info", "warning", etc.)
            structlog.stdlib.add_log_level,
            # Add logger name (module path)
            structlog.stdlib.add_logger_name,
            # Add ISO 8601 timestamp
            structlog.processors.TimeStamper(fmt="iso"),
            # Render Python exception tracebacks
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Ensure all byte strings are decoded
            structlog.processors.UnicodeDecoder(),
            # Final renderer (JSON or pretty console)
            renderer,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure Python's built-in logging module to also use structlog
    # This ensures third-party libraries (uvicorn, SQLAlchemy, etc.) also
    # have their logs processed through our structured pipeline.
    log_level = logging.DEBUG if settings.DEBUG else logging.INFO
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # Silence noisy loggers in development
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.DEBUG if settings.DEBUG else logging.WARNING
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a named logger for a module.

    Usage:
        logger = get_logger(__name__)
        logger.info("something_happened", key="value")
    """
    return structlog.get_logger(name)