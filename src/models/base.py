"""
models/base.py — SQLAlchemy Declarative Base & Shared Mixins
=============================================================

WHY A SEPARATE BASE FILE?
--------------------------
Every SQLAlchemy model must inherit from the same DeclarativeBase.
Keeping it here (instead of in database/core.py) prevents circular
imports: models import Base, but database/core.py should not need to
import models.

WHAT IS DeclarativeBase?
-------------------------
SQLAlchemy 2.0 introduced the new `DeclarativeBase` class.
It replaces the old `declarative_base()` factory function.

Each class that inherits from `Base` becomes a database table.
SQLAlchemy reads the class attributes (Mapped[type]) and generates
the SQL CREATE TABLE statement automatically.

MIXINS
------
A mixin is a plain Python class (no table of its own) that provides
reusable columns to multiple models via multiple inheritance.

TimestampMixin adds `created_at` and `updated_at` to every model
that needs them — one definition, used everywhere.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ------------------------------------------------------------------ #
# Declarative Base
# ------------------------------------------------------------------ #
class Base(DeclarativeBase):
    """
    Root base class for all ORM models.

    All models inherit from this:
        class User(Base):
            __tablename__ = "users"
            ...

    Alembic uses Base.metadata to discover all tables and generate
    migrations. That is why alembic/env.py imports Base.
    """
    pass


# ------------------------------------------------------------------ #
# Timestamp Mixin
# ------------------------------------------------------------------ #
class TimestampMixin:
    """
    Adds `created_at` and `updated_at` columns to any model.

    Usage:
        class Case(TimestampMixin, Base):
            __tablename__ = "cases"
            ...
            # created_at and updated_at are automatically added

    created_at:
        Set once when the row is inserted. Never changes.
        Uses the database server's current time (server_default=func.now())
        so it doesn't depend on the application server's clock.

    updated_at:
        Updated automatically every time the row changes
        (onupdate=func.now()). Useful for "last modified" displays
        and cache invalidation.

    timezone=True:
        Stores timestamps with timezone info (UTC).
        Always use UTC in the database — convert to local time
        in the Flutter app based on the user's device timezone.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ------------------------------------------------------------------ #
# UUID Helper
# ------------------------------------------------------------------ #
def new_uuid() -> str:
    """
    Generate a new UUID4 string.

    We store UUIDs as strings (VARCHAR 36) rather than native UUID
    columns for maximum compatibility with SQLite (used in tests)
    and PostgreSQL (used in production).

    PostgreSQL has a native UUID type, but SQLite does not.
    Using strings means the same model works for both without
    conditional logic — critical for our test/prod strategy.
    """
    return str(uuid.uuid4())
