"""
workers/celery_app.py — Celery Application
==========================================

WHY CELERY?
-----------
AI analysis (Gemini calls, GCS downloads) takes 10-60 seconds per case.
Running that inside a FastAPI request would:
  - Block the async event loop (sync Gemini SDK calls)
  - Time out the HTTP connection
  - Prevent proper error recovery and retries

Celery offloads these to background workers. The API responds immediately
with a task ID; the Flutter app polls GET /ai/status to check progress.

REDIS AS BROKER + BACKEND
--------------------------
- Broker: holds the task queue (worker picks tasks from here)
- Backend: stores task results (PENDING → STARTED → SUCCESS/FAILURE)
Both use the same REDIS_URL. In production, use separate Redis DBs
(e.g., /0 for broker, /1 for results) to avoid key collisions.

TASK SETTINGS
-------------
- task_acks_late=True: task message is not acknowledged until it completes.
  If the worker crashes mid-task, the message is re-queued for another worker.
- worker_prefetch_multiplier=1: each worker fetches one task at a time.
  This prevents a slow AI task from blocking other tasks on the same worker.
- task_track_started=True: enables the STARTED state, so we can show
  "processing" in the status endpoint even before the task finishes.

GRACEFUL DEGRADATION
--------------------
If Redis is not running, importing this module is fine — Celery is lazy.
Tasks will fail at enqueue time (connection refused), which the service
catches and converts to a clear error response.
"""

from celery import Celery
from celery.signals import worker_process_init

from src.config import settings


@worker_process_init.connect
def _init_worker_logging(**kwargs):
    """
    Called once per worker process when it starts.
    Wires up structlog + file handler so Celery task logs
    go to logs/app.log alongside the API server logs.
    """
    import logging
    from src.logger import setup_logging
    setup_logging()

    # Silence noisy Celery internals from polluting the log file
    logging.getLogger("celery").setLevel(logging.WARNING)
    logging.getLogger("celery.worker.strategy").setLevel(logging.WARNING)
    logging.getLogger("celery.app.trace").setLevel(logging.WARNING)
    logging.getLogger("amqp").setLevel(logging.WARNING)


celery_app = Celery(
    "aiderm_cliniq",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "src.workers.tasks.analysis",
        "src.workers.tasks.questions",
        "src.workers.tasks.reports",
        "src.workers.tasks.email",
    ],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Reliability
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,

    # Result expiry — keep results 24 hours (enough for polling)
    result_expires=86400,
)
