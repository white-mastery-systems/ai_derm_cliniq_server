"""
config.py — Application Settings
=================================
All environment variables are declared here as typed fields.
Pydantic's BaseSettings reads them from:
  1. The process environment (os.environ)
  2. A .env file in the project root (via env_file setting)

WHY THIS PATTERN?
-----------------
Centralising config here means:
- Every setting is typed and validated at startup (fail fast).
- No magic strings scattered across the codebase.
- Easy to test by overriding values in fixtures.
- One import: `from src.config import settings`

HOW TO USE:
-----------
    from src.config import settings

    print(settings.DATABASE_URL)
    print(settings.ACCESS_TOKEN_EXPIRE_MINUTES)
"""

from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All application settings.

    Fields with defaults are optional.
    Fields without defaults MUST be set in .env or the environment —
    the app will refuse to start if they are missing.
    """

    # ------------------------------------------------------------------ #
    # App
    # ------------------------------------------------------------------ #
    APP_ENV: str = "development"
    APP_NAME: str = "AiDerm Cliniq API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # ------------------------------------------------------------------ #
    # Security / JWT
    # ------------------------------------------------------------------ #
    # IMPORTANT: Change this to a long random string in production.
    # Generate one with: python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60          # 1 hour
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30            # 30 days

    # ------------------------------------------------------------------ #
    # Database (PostgreSQL)
    # ------------------------------------------------------------------ #
    # Format: postgresql+asyncpg://user:password@host:port/dbname
    # asyncpg is the async PostgreSQL driver used by SQLAlchemy async.
    DATABASE_URL: str

    # A synchronous URL is needed only for Alembic migrations (which run
    # in a non-async context). We derive it from DATABASE_URL automatically.
    @property
    def SYNC_DATABASE_URL(self) -> str:
        """Replace 'asyncpg' with 'psycopg2' for Alembic compatibility."""
        return self.DATABASE_URL.replace("+asyncpg", "+psycopg2")

    # ------------------------------------------------------------------ #
    # Redis (Celery broker + result backend + cache)
    # ------------------------------------------------------------------ #
    REDIS_URL: str = "redis://localhost:6379/0"

    # ------------------------------------------------------------------ #
    # Google Cloud Storage
    # ------------------------------------------------------------------ #
    GOOGLE_APPLICATION_CREDENTIALS: str = ""   # Path to service account JSON
    GCS_BUCKET_NAME: str = "aiderm-cliniq-storage"

    # ------------------------------------------------------------------ #
    # Google OAuth 2.0
    # ------------------------------------------------------------------ #
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/google/callback"

    # ------------------------------------------------------------------ #
    # AI Provider Keys
    # ------------------------------------------------------------------ #
    GEMINI_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    PERPLEXITY_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""

    # Default LLM provider. Can be "gemini", "openai", "perplexity", "deepseek"
    DEFAULT_LLM_PROVIDER: str = "gemini"

    # ------------------------------------------------------------------ #
    # AI Model Names (overridable from admin panel at runtime via Redis)
    # ------------------------------------------------------------------ #
    # These are the DEFAULT model names loaded at startup from .env.
    # The admin panel can override them at runtime — see src/ai/model_registry.py.
    GEMINI_MODEL: str = "gemini-2.5-flash"
    OPENAI_MODEL: str = "gpt-4o"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # ------------------------------------------------------------------ #
    # Email (Gmail SMTP)
    # ------------------------------------------------------------------ #
    GMAIL_USER: str = ""
    GMAIL_APP_PASSWORD: str = ""
    FRONTEND_URL: str = "https://aidermcliniq.com"   # Base URL embedded in visit emails

    # ------------------------------------------------------------------ #
    # CORS
    # ------------------------------------------------------------------ #
    # List of allowed origins. Use ["*"] for development only.
    CORS_ORIGINS: List[str] = ["*"]

    # ------------------------------------------------------------------ #
    # Rate Limiting
    # ------------------------------------------------------------------ #
    RATE_LIMIT_PER_MINUTE: int = 60

    # ------------------------------------------------------------------ #
    # File Upload Constraints
    # ------------------------------------------------------------------ #
    MAX_IMAGE_SIZE_MB: int = 10
    MAX_IMAGES_PER_CASE: int = 10
    ALLOWED_IMAGE_TYPES: List[str] = ["image/jpeg", "image/png", "image/heic", "image/heif"]

    # ------------------------------------------------------------------ #
    # QR Token
    # ------------------------------------------------------------------ #
    QR_TOKEN_EXPIRE_HOURS: int = 24

    # ------------------------------------------------------------------ #
    # Pydantic Settings Configuration
    # ------------------------------------------------------------------ #
    model_config = SettingsConfigDict(
        env_file=".env",           # Read from .env in the working directory
        env_file_encoding="utf-8",
        case_sensitive=True,       # DATABASE_URL ≠ database_url
        extra="ignore",            # Silently ignore unknown env vars
    )

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("APP_ENV")
    @classmethod
    def validate_app_env(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"APP_ENV must be one of {allowed}, got '{v}'")
        return v

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @property
    def max_image_size_bytes(self) -> int:
        return self.MAX_IMAGE_SIZE_MB * 1024 * 1024


# ------------------------------------------------------------------ #
# Singleton — module-level cached instance
# ------------------------------------------------------------------ #
# @lru_cache ensures this is only instantiated once per process.
# This is important: reading .env is not free, and we want a single
# shared Settings object everywhere.
#
# Usage:
#   from src.config import settings
#   settings.DATABASE_URL  ← always the same object
@lru_cache
def get_settings() -> Settings:
    return Settings()


# Convenience alias — most files just do `from src.config import settings`
#
# WHY NOT module-level instantiation?
# ------------------------------------
# settings = get_settings()  ← this would run at import time.
# During tests, pytest-env hasn't injected env vars yet when Python
# first imports this module. This causes ValidationError for missing
# SECRET_KEY / DATABASE_URL.
#
# Instead we use a module-level proxy: the first attribute access calls
# get_settings(), which by then has the env vars from pytest-env or the
# test conftest.py. After that, lru_cache returns the same object.
#
# All existing code that does `from src.config import settings` works
# identically — settings.DATABASE_URL, settings.SECRET_KEY, etc.
class _SettingsProxy:
    """
    Lazy proxy for the Settings singleton.
    Defers instantiation to first attribute access instead of import time.
    This makes tests work without pre-loading environment variables.
    """
    _instance: Settings | None = None

    def _get(self) -> Settings:
        if self._instance is None:
            self._instance = get_settings()
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._get(), name)

    def __repr__(self) -> str:
        return repr(self._get())


settings: Settings = _SettingsProxy()  # type: ignore[assignment]
