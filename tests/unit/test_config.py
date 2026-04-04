"""
tests/unit/test_config.py — Unit Tests for Configuration
==========================================================

WHAT WE'RE TESTING
-------------------
1. Settings loads correctly from environment variables.
2. Computed properties work correctly (SYNC_DATABASE_URL, is_production, etc.).
3. Invalid APP_ENV raises a validation error.
4. Singleton caching (get_settings returns the same object).

LESSON: Unit tests for config are fast and catch mistakes like:
- Typos in field names
- Wrong default values
- Broken computed properties
"""

import pytest
from pydantic import ValidationError

from src.config import Settings, get_settings, settings


class TestSettingsDefaults:
    """Test that default values are correct."""

    def test_default_algorithm(self):
        """JWT algorithm should default to HS256."""
        s = Settings(SECRET_KEY="test-key", DATABASE_URL="postgresql+asyncpg://u:p@h/db")
        assert s.ALGORITHM == "HS256"

    def test_default_access_token_expire(self):
        """Access token should default to 60 minutes."""
        s = Settings(SECRET_KEY="test-key", DATABASE_URL="postgresql+asyncpg://u:p@h/db")
        assert s.ACCESS_TOKEN_EXPIRE_MINUTES == 60

    def test_default_refresh_token_expire(self):
        """Refresh token should default to 30 days."""
        s = Settings(SECRET_KEY="test-key", DATABASE_URL="postgresql+asyncpg://u:p@h/db")
        assert s.REFRESH_TOKEN_EXPIRE_DAYS == 30

    def test_default_app_env(self):
        """Default environment should be 'development'."""
        s = Settings(SECRET_KEY="test-key", DATABASE_URL="postgresql+asyncpg://u:p@h/db")
        assert s.APP_ENV == "development"

    def test_default_cors_origins(self):
        """CORS should default to allow all origins."""
        s = Settings(SECRET_KEY="test-key", DATABASE_URL="postgresql+asyncpg://u:p@h/db")
        assert s.CORS_ORIGINS == ["*"]

    def test_default_max_image_size(self):
        """Max image size should default to 10 MB."""
        s = Settings(SECRET_KEY="test-key", DATABASE_URL="postgresql+asyncpg://u:p@h/db")
        assert s.MAX_IMAGE_SIZE_MB == 10


class TestComputedProperties:
    """Test properties that are derived from other settings."""

    def test_sync_database_url(self):
        """
        SYNC_DATABASE_URL should replace asyncpg with psycopg2.
        Alembic needs a sync driver, so we auto-derive this from DATABASE_URL.
        """
        s = Settings(
            SECRET_KEY="test-key",
            DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/mydb",
        )
        assert s.SYNC_DATABASE_URL == "postgresql+psycopg2://user:pass@localhost:5432/mydb"

    def test_is_production_true(self):
        s = Settings(
            SECRET_KEY="test-key",
            DATABASE_URL="postgresql+asyncpg://u:p@h/db",
            APP_ENV="production",
        )
        assert s.is_production is True
        assert s.is_development is False

    def test_is_development_true(self):
        s = Settings(
            SECRET_KEY="test-key",
            DATABASE_URL="postgresql+asyncpg://u:p@h/db",
            APP_ENV="development",
        )
        assert s.is_development is True
        assert s.is_production is False

    def test_max_image_size_bytes(self):
        """10 MB should be 10 * 1024 * 1024 bytes."""
        s = Settings(SECRET_KEY="test-key", DATABASE_URL="postgresql+asyncpg://u:p@h/db")
        assert s.max_image_size_bytes == 10 * 1024 * 1024


class TestValidation:
    """Test that invalid values are caught at startup, not at runtime."""

    def test_invalid_app_env_raises(self):
        """
        APP_ENV must be development, staging, or production.
        Anything else should raise a ValidationError immediately.
        This is the 'fail fast' principle — bad config = no server start.
        """
        with pytest.raises(ValidationError) as exc_info:
            Settings(
                SECRET_KEY="test-key",
                DATABASE_URL="postgresql+asyncpg://u:p@h/db",
                APP_ENV="invalid_env",
            )
        # Check the error mentions what's wrong
        assert "APP_ENV" in str(exc_info.value)

    def test_missing_secret_key_raises(self, monkeypatch):
        """
        SECRET_KEY has no default, so Settings() without it must fail.

        WHY monkeypatch.delenv + _env_file=None?
        -----------------------------------------
        Two sources can supply SECRET_KEY:
        1. os.environ — monkeypatch.delenv removes it
        2. The project .env file — pydantic-settings reads this by default

        We must neutralise both. Passing _env_file=None tells
        pydantic-settings to skip the .env file for this call only,
        ensuring the ValidationError actually fires.
        monkeypatch restores the original env var after the test.
        """
        monkeypatch.delenv("SECRET_KEY", raising=False)
        with pytest.raises(ValidationError):
            Settings(DATABASE_URL="postgresql+asyncpg://u:p@h/db", _env_file=None)

    def test_missing_database_url_raises(self, monkeypatch):
        """DATABASE_URL has no default, so omitting it must fail."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ValidationError):
            Settings(SECRET_KEY="test-key", _env_file=None)


class TestSingleton:
    """Test the lru_cache singleton pattern."""

    def test_get_settings_returns_same_object(self):
        """
        get_settings() should return the exact same Settings instance
        every time (lru_cache). This prevents re-reading .env on every call.
        """
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2  # `is` checks object identity, not just equality

    def test_module_level_settings_resolves_to_get_settings(self):
        """
        `from src.config import settings` is a _SettingsProxy.
        Its internal ._get() must return the exact same cached Settings
        object as get_settings() — proving the proxy wraps the singleton.

        WHY A PROXY INSTEAD OF DIRECT ASSIGNMENT?
        ------------------------------------------
        settings = get_settings() at module level would instantiate
        Settings() at import time, before test env vars are injected.
        The proxy defers instantiation to first attribute access,
        solving the test-time import order problem.
        """
        from src.config import _SettingsProxy
        assert isinstance(settings, _SettingsProxy)
        assert settings._get() is get_settings()
