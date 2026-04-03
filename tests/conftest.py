"""
tests/conftest.py — Shared Test Fixtures
==========================================

IMPORTANT — WHY os.environ IS SET FIRST
-----------------------------------------
`src/config.py` uses Pydantic BaseSettings which reads env vars at
instantiation time. The module-level `settings` proxy defers that
instantiation, BUT the very first import of `src.main` or `src.config`
that triggers Settings() must already have the env vars set.

We set os.environ HERE, at the top of conftest.py, before any src.*
imports. This guarantees the env vars are present when Settings() is
called for the first time, regardless of import order.

WHAT IS conftest.py?
---------------------
pytest automatically loads this file before any test runs.
Functions decorated with @pytest.fixture become *fixtures* —
reusable setup/teardown helpers that tests can request by name.

FIXTURE SCOPES
--------------
- scope="function" (default): Fresh instance for every test.
- scope="module":  One instance shared across the entire test module.
- scope="session": One instance for the entire test run.
"""

# ------------------------------------------------------------------ #
# STEP 1: Inject test environment variables BEFORE any src.* imports.
# These override (or provide) values that Pydantic Settings requires.
# ------------------------------------------------------------------ #
import os

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-production-32chars")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("DEBUG", "false")

# ------------------------------------------------------------------ #
# STEP 2: Now safe to import src modules.
# ------------------------------------------------------------------ #
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config import settings
from src.main import create_app

# Import Base after env vars are set so models register their metadata.
# This import MUST come after os.environ setup.
from src.models import Base  # noqa: E402

# ------------------------------------------------------------------ #
# Use an in-memory SQLite database for tests.
# This means tests:
# - Don't need a running PostgreSQL instance
# - Run extremely fast (no network)
# - Are fully isolated (fresh DB per test session)
#
# Trade-off: SQLite != PostgreSQL (some queries may differ).
# For critical DB logic, use a real PostgreSQL test database.
# ------------------------------------------------------------------ #
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """
    Create a test database engine (SQLite in-memory).
    Creates ALL tables from Base.metadata before yielding.
    Shared across the entire test session for speed.

    WHY StaticPool?
    ---------------
    SQLite in-memory databases are connection-scoped.
    With a normal pool, each connection gets a fresh empty database.
    StaticPool forces all sessions to share the same connection,
    so the tables created in setup are visible to all tests.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    # Create all tables defined in our models
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Drop all tables and dispose engine after test session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """
    Provide a database session per test.
    Each test gets a fresh transaction that is rolled back at the end,
    ensuring tests don't pollute each other's data.
    """
    session_factory = async_sessionmaker(
        bind=test_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def app_client() -> AsyncGenerator[AsyncClient, None]:
    """
    An async HTTP client that talks directly to the FastAPI app
    (no real network — uses ASGI transport).

    Use this in integration tests to test full request/response cycles:

        async def test_health(app_client):
            response = await app_client.get("/health")
            assert response.status_code == 200
    """
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.fixture
def override_settings(monkeypatch):
    """
    Helper to temporarily override settings values in a test.

    Usage:
        def test_something(override_settings):
            override_settings("DEBUG", True)
            override_settings("RATE_LIMIT_PER_MINUTE", 1000)
    """
    def _override(field: str, value):
        monkeypatch.setattr(settings, field, value)

    return _override
