"""
alembic/env.py — Alembic Migration Environment
================================================

This file is loaded by Alembic every time you run a migration command.
It connects Alembic to our application's database and models.

TWO MODES
----------
Alembic can run migrations in two modes:

1. OFFLINE mode (--sql flag):
   Generates SQL statements to a file without connecting to the DB.
   Useful for generating migration scripts to review or run manually.

2. ONLINE mode (default):
   Connects to the database and runs migrations directly.
   This is what you use in production and development.

ASYNC ENGINE + run_sync
------------------------
Our application uses an async SQLAlchemy engine (asyncpg).
Alembic's migration runner is synchronous — it cannot directly use
an async engine.

Solution: use `connectable.connect()` in a sync context.
We create an async engine, then use `run_sync` to execute the
synchronous Alembic migration runner inside the async connection.

SYNC DATABASE URL
-----------------
We use `settings.SYNC_DATABASE_URL` (postgresql+psycopg2://...)
instead of the app's async URL (postgresql+asyncpg://...) because
Alembic's online mode needs a synchronous driver.

HOW TO USE
----------
From the project root (ai_derm_cliniq_server/):

  # Generate a new migration from model changes:
  alembic revision --autogenerate -m "add_user_table"

  # Apply all pending migrations:
  alembic upgrade head

  # Roll back one migration:
  alembic downgrade -1

  # See current migration state:
  alembic current

  # See migration history:
  alembic history
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

# ------------------------------------------------------------------ #
# Alembic Config
# ------------------------------------------------------------------ #
# `config` is the Alembic Config object, which provides access to
# values in alembic.ini.
config = context.config

# Set up Python logging using the alembic.ini [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ------------------------------------------------------------------ #
# Import models & Base — CRITICAL for --autogenerate
# ------------------------------------------------------------------ #
# Importing src.models triggers all model class definitions.
# Without this, Base.metadata has no tables and --autogenerate
# produces an empty migration.
from src.models import Base  # noqa: E402  (import after sys.path setup)
from src.config import settings  # noqa: E402

# Tell Alembic to use our models' metadata for autogeneration.
target_metadata = Base.metadata

# ------------------------------------------------------------------ #
# Database URL
# ------------------------------------------------------------------ #
# We use the synchronous URL (psycopg2) for Alembic.
# The app itself uses the async URL (asyncpg) — see database/core.py.
def get_url() -> str:
    return settings.SYNC_DATABASE_URL


# ------------------------------------------------------------------ #
# OFFLINE mode
# ------------------------------------------------------------------ #
def run_migrations_offline() -> None:
    """
    Run migrations without a live DB connection.
    Outputs SQL to stdout or a file.

    Useful for:
    - Reviewing migrations before applying
    - Generating SQL for a DBA to run manually
    - CI/CD pipelines that don't have DB access
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Compare server defaults to detect changes in DEFAULT values
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ------------------------------------------------------------------ #
# ONLINE mode (synchronous part — called via run_sync)
# ------------------------------------------------------------------ #
def do_run_migrations(connection) -> None:
    """
    Execute migrations using an active DB connection.
    Called by run_migrations_online() inside run_sync().
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_server_default=True,
        # Compare types to detect column type changes
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ------------------------------------------------------------------ #
# ONLINE mode (async wrapper)
# ------------------------------------------------------------------ #
async def run_migrations_online() -> None:
    """
    Run migrations against a live database using an async engine.

    We create a temporary async engine just for Alembic.
    The URL uses psycopg2 (sync driver) so we need aiosqlite/asyncpg
    only for the application engine, not here.

    Actually for Alembic we use the sync URL with a sync engine
    wrapped in run_sync. This is the standard pattern for SQLAlchemy
    2.0 + Alembic.
    """
    from sqlalchemy import create_engine

    # Use sync engine for Alembic (psycopg2 / aiosqlite not needed here)
    connectable = create_engine(
        get_url(),
        poolclass=pool.NullPool,  # Don't pool — each migration run is one connection
    )

    with connectable.connect() as connection:
        do_run_migrations(connection)

    connectable.dispose()


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
