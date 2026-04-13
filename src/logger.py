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
import logging.handlers
import sys
from pathlib import Path

import structlog

from src.config import settings

# Logs folder sits next to src/ in the project root
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def _build_file_handler() -> logging.handlers.RotatingFileHandler:
    """
    Rotating file handler — writes JSON lines to logs/app.log.

    Rotation:   10 MB per file, keeps last 5 files.
    Files:      logs/app.log  (active)
                logs/app.log.1  …  logs/app.log.5  (rotated)
    """
    LOGS_DIR.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        filename=LOGS_DIR / "app.log",
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def setup_logging() -> None:
    """
    Configure structlog and Python's standard logging.
    Call this ONCE at application startup (in main.py lifespan).

    Output:
      - Terminal  → colourful console (dev) or JSON (prod)
      - logs/app.log → always JSON, rotating, 10 MB × 5 files
    """

    # ── Structlog processors shared by both outputs ──────────────────
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    # ── Terminal renderer ─────────────────────────────────────────────
    if settings.is_development:
        console_renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        console_renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [console_renderer],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ── Python stdlib logging (uvicorn, SQLAlchemy, Celery, etc.) ────
    log_level = logging.DEBUG if settings.DEBUG else logging.INFO

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove any handlers set by basicConfig / previous calls
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(console_handler)

    # File handler — always JSON so it's machine-readable
    file_handler = _build_file_handler()
    root_logger.addHandler(file_handler)

    # Silence noisy loggers
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