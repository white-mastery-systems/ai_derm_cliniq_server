"""
database/core.py — Async Database Engine & Session Factory
===========================================================

KEY CONCEPTS
------------

1. Engine
   The engine manages a pool of PostgreSQL connections.
   We create it ONCE at startup and share it across the whole app.
   Using `asyncpg` as the driver means all DB I/O is non-blocking —
   the event loop never stalls waiting for a query to finish.

2. Session (AsyncSession)
   A session represents ONE unit of work (typically one HTTP request).
   All your ORM queries within a request share the same session.
   The session is committed at the end of a successful request,
   or rolled back if an exception occurs.

3. get_async_session (the dependency)
   This is a FastAPI dependency. Every route that needs DB access
   declares it in its signature:

       async def my_route(db: AsyncSession = Depends(get_async_session)):
           result = await db.execute(...)

   FastAPI automatically calls this function, yields the session to
   the route, then runs the cleanup (commit/rollback/close) after
   the response is sent.

4. Why async?
   A standard (sync) SQLAlchemy session BLOCKS the thread while waiting
   for PostgreSQL. With async, the event loop can handle other requests
   while the DB query is running. This is essential for high-concurrency
   APIs like ours.

LIFECYCLE
---------
   App startup  →  create_engine()  →  pool of connections open
   HTTP request →  get_async_session() yields a session
   Route runs   →  queries run against the session
   Response sent →  session.commit() or session.rollback()
   App shutdown  →  engine.dispose()  →  connections closed
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings


# ------------------------------------------------------------------ #
# Engine factory
# ------------------------------------------------------------------ #
def _build_engine() -> AsyncEngine:
    """
    Build the async SQLAlchemy engine.

    WHY A FACTORY FUNCTION?
    -----------------------
    Creating the engine at module level (engine = create_async_engine(...))
    causes two problems:

    1. It reads settings.DATABASE_URL at IMPORT time — before test env
       vars are injected, causing failures in the test suite.

    2. PostgreSQL connection-pool arguments (pool_size, max_overflow) are
       not valid for SQLite, which is used in tests. Building the engine
       inside a function lets us detect the database type and pass only
       the arguments that are appropriate.

    pool_pre_ping=True (PostgreSQL only):
        Before giving a connection from the pool to a session, SQLAlchemy
        sends a lightweight SELECT 1 to confirm the connection is alive.
        Prevents "connection closed" errors after PostgreSQL restarts.

    pool_size / max_overflow (PostgreSQL only):
        Keep up to 10 persistent connections.
        Allow 20 extra overflow connections during traffic spikes.

    echo=settings.DEBUG:
        When DEBUG=True, every SQL statement is printed to stdout.
        Disable in production.
    """
    db_url: str = settings.DATABASE_URL
    is_sqlite = db_url.startswith("sqlite")

    if is_sqlite:
        # SQLite does not support connection pooling arguments.
        # StaticPool keeps one connection open for the lifetime of the
        # engine — correct behaviour for in-memory test databases.
        from sqlalchemy.pool import StaticPool
        return create_async_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )

    return create_async_engine(
        db_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        echo=settings.DEBUG,
    )


# Module-level engine — built on first access via _build_engine().
# main.py calls engine.dispose() on shutdown.
engine: AsyncEngine = _build_engine()

# ------------------------------------------------------------------ #
# Session Factory
# ------------------------------------------------------------------ #
# async_sessionmaker creates new AsyncSession instances.
# expire_on_commit=False:
#   After a commit, SQLAlchemy normally "expires" all ORM objects,
#   meaning the next attribute access triggers a lazy DB query.
#   In async code, lazy loading is not supported.
#   Setting this to False keeps the object data in memory after commit,
#   so you can still read attributes without another DB round-trip.
AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,   # We control when to flush manually
    autocommit=False,  # We always commit explicitly
)


# ------------------------------------------------------------------ #
# Dependency — used in FastAPI route signatures
# ------------------------------------------------------------------ #
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that provides a database session per request.

    Usage in a route:
        from src.database.core import get_async_session

        @router.get("/example")
        async def example(db: AsyncSession = Depends(get_async_session)):
            result = await db.execute(select(MyModel))
            return result.scalars().all()

    The `async with` block guarantees:
    - On success: session is committed and closed.
    - On exception: session is rolled back and closed.
    - Either way: the connection is returned to the pool.
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ------------------------------------------------------------------ #
# Health Check Helper
# ------------------------------------------------------------------ #
async def check_database_connection() -> bool:
    """
    Ping the database. Used by the /health endpoint.
    Returns True if the database is reachable, False otherwise.
    """
    from sqlalchemy import text

    try:
        async with AsyncSessionFactory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
