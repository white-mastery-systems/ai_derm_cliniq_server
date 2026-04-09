"""
models/verification_token.py — Email Verification & Password Reset Tokens
==========================================================================

One model handles two purposes:
  - PASSWORD_RESET : patient clicks "Forgot Password" → receives email link
  - EMAIL_VERIFY   : new registration → receives email link to confirm address

WHY ONE MODEL FOR BOTH?
------------------------
Both flows share identical mechanics:
  1. Generate a secure random token
  2. Hash it and store in DB with expiry
  3. Email the raw token as a link to the user
  4. On click: look up by hash, check expiry, mark used, take action

Keeping them in one table avoids duplicating the same structure twice.
The `purpose` column differentiates the two.

TOKEN LIFECYCLE
---------------
  generated → emailed → clicked → used=True
  If not clicked before expires_at → token is invalid (not deleted)

SECURITY NOTES
--------------
- Only the SHA-256 hash is stored (same pattern as RefreshToken).
- Raw token is never persisted — only emailed.
- Tokens are single-use: `used=True` after first valid redemption.
- Expiry: 15 min for password reset, 24h for email verification.
"""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class TokenPurpose(str, enum.Enum):
    PASSWORD_RESET = "password_reset"
    EMAIL_VERIFY = "email_verify"


class VerificationToken(TimestampMixin, Base):
    __tablename__ = "verification_tokens"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Foreign Key
    # ------------------------------------------------------------------ #
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------ #
    # Token
    # ------------------------------------------------------------------ #
    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        comment="SHA-256 hex digest of the raw token. Never store the raw token.",
    )

    # ------------------------------------------------------------------ #
    # Purpose & Lifecycle
    # ------------------------------------------------------------------ #
    purpose: Mapped[TokenPurpose] = mapped_column(
        Enum(TokenPurpose, name="token_purpose_enum", create_type=True, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        comment="password_reset | email_verify",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    used: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ------------------------------------------------------------------ #
    # Relationship
    # ------------------------------------------------------------------ #
    user: Mapped["User"] = relationship("User")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<VerificationToken user={self.user_id!r} "
            f"purpose={self.purpose} used={self.used}>"
        )
