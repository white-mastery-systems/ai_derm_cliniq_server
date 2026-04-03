"""
main.py — FastAPI Application Entry Point
==========================================

This file does four things:
1. Defines the lifespan (startup + shutdown logic)
2. Creates the FastAPI instance
3. Attaches all middleware
4. Registers all routers via api.py

LIFESPAN (contextmanager pattern)
-----------------------------------
FastAPI replaced the old @app.on_event("startup") / @app.on_event("shutdown")
decorators with a single `lifespan` async context manager.

Everything BEFORE `yield` runs on startup.
Everything AFTER `yield` runs on shutdown.

    startup:  setup logging, check DB connection, log app info
    shutdown: dispose DB connection pool, flush logs

WHY NOT PUT LOGIC DIRECTLY IN MAIN.PY?
----------------------------------------
Each concern (logging, DB, routes) lives in its own module.
main.py just *connects* them. This keeps main.py short and
makes each module independently testable.

RUNNING THE SERVER
-------------------
    uvicorn src.main:app --reload --port 8000

    --reload  : Auto-restart when code changes (development only)
    --port    : Listen on port 8000
    src.main  : The `app` object inside src/main.py

AUTO-GENERATED DOCS
--------------------
Once running, visit:
    http://localhost:8000/docs    ← Swagger UI (interactive)
    http://localhost:8000/redoc  ← ReDoc (read-only, clean)
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src.api import include_all_routers
from src.config import settings
from src.database.core import check_database_connection, engine
from src.exceptions import register_exception_handlers
from src.logger import get_logger, setup_logging
from src.rate_limiting import limiter

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# Lifespan
# ------------------------------------------------------------------ #
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.
    Code before `yield` = startup.
    Code after `yield` = shutdown.
    """
    # --- STARTUP ---
    setup_logging()

    logger.info(
        "app_starting",
        name=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.APP_ENV,
    )

    # Verify the database is reachable before accepting traffic.
    # If this fails, the server starts but logs a critical warning.
    db_ok = await check_database_connection()
    if db_ok:
        logger.info("database_connected")
    else:
        logger.critical("database_unreachable", url=settings.DATABASE_URL)

    logger.info("app_ready", docs_url="http://localhost:8000/docs")

    yield  # ← Server is running and handling requests here

    # --- SHUTDOWN ---
    logger.info("app_shutting_down")
    # Close all connections in the SQLAlchemy pool gracefully.
    await engine.dispose()
    logger.info("database_pool_closed")


# ------------------------------------------------------------------ #
# App Instance
# ------------------------------------------------------------------ #
def create_app() -> FastAPI:
    """
    Factory function that creates and configures the FastAPI application.

    Using a factory (instead of a bare module-level `app = FastAPI()`)
    makes it easy to create isolated app instances in tests.
    """
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=(
            "AI-powered dermatology diagnostic API. "
            "Flutter mobile app backend for AiDerm Cliniq."
        ),
        # In production, hide the interactive docs from public access.
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------ #
    # Middleware (order matters — applied bottom-up to requests)
    # ------------------------------------------------------------------ #

    # CORS: Allow Flutter app to call this API from any origin.
    # In production, replace ["*"] with your actual domain(s).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Rate limiting middleware (uses slowapi + Redis)
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ------------------------------------------------------------------ #
    # Exception Handlers
    # ------------------------------------------------------------------ #
    register_exception_handlers(app)

    # ------------------------------------------------------------------ #
    # Routes
    # ------------------------------------------------------------------ #
    include_all_routers(app)

    # Health check endpoint — used by Docker/Kubernetes to verify the
    # container is alive. No auth required.
    @app.get("/health", tags=["Health"], summary="Health check")
    async def health_check() -> dict:
        db_ok = await check_database_connection()
        return {
            "status": "healthy" if db_ok else "degraded",
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "environment": settings.APP_ENV,
            "database": "ok" if db_ok else "unreachable",
        }

    return app


# ------------------------------------------------------------------ #
# Module-level app instance
# ------------------------------------------------------------------ #
# `uvicorn src.main:app` looks for this name at module level.
app: FastAPI = create_app()
