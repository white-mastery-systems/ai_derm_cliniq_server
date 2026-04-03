"""
models/refresh_token.py — JWT Refresh Token Store
==================================================

Stores hashed refresh tokens so they can be revoked server-side.

WHY STORE REFRESH TOKENS IN THE DB?
-------------------------------------
JWTs are stateless — once issued, the server cannot invalidate them
until they expire. For short-lived ACCESS tokens (60 min) this is fine.
But REFRESH tokens last 30 days — if stolen, an attacker has 30-day access.

Solution: store a hash of each refresh token in the DB.
On every token refresh request:
1. Client sends the refresh token
2. Server hashes it and looks up this table
3. If `revoked = True` → reject immediately (no 30-day window for attacker)
4. If expired → reject
5. If valid → issue new access + refresh token, revoke the old one

WHY STORE THE HASH, NOT THE TOKEN?
------------------------------------
If the DB is breached, raw tokens would give instant access.
Hashed tokens are useless without the original — same principle as
storing password hashes instead of plaintext passwords.

We use SHA-256 (not bcrypt) for token hashing because:
- We need to look up by hash (requires constant-time hash, not bcrypt's variable cost)
- Tokens are already high-entropy random strings — no need for bcrypt's salting
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class RefreshToken(TimestampMixin, Base):
    """
    One issued refresh token per login session.
    """

    __tablename__ = "refresh_tokens"

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
        comment="The user this token was issued to",
    )

    # ------------------------------------------------------------------ #
    # Token
    # ------------------------------------------------------------------ #
    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        comment="SHA-256 hex digest of the raw refresh token. "
                "Never store the raw token.",
    )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Token is invalid after this UTC datetime (default: now + 30 days)",
    )
    revoked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
        comment="True after logout or token rotation. Reject immediately.",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the token was revoked (logout / rotation)",
    )

    # ------------------------------------------------------------------ #
    # Relationship
    # ------------------------------------------------------------------ #
    user: Mapped["User"] = relationship(  # noqa: F821
        "User",
        back_populates="refresh_tokens",
    )

    def __repr__(self) -> str:
        return (
            f"<RefreshToken user={self.user_id!r} "
            f"revoked={self.revoked} "
            f"expires={self.expires_at}>"
        )
