"""
models/qr_token.py — QR Code Token
=====================================

Stores QR tokens generated at the end of the patient consultation.
The doctor scans this QR code to open the case on their device.

FLOW (from Figma Basic Patient Flow)
-------------------------------------
1. Patient completes consultation → Case Summary shown
2. Backend generates a QRToken linked to the case
3. QR code image (containing the token) is shown to patient
4. Patient shows phone to doctor
5. Doctor scans QR → GET /api/v1/qr/scan/{token}
6. Backend validates: not used, not expired → returns case_id
7. Doctor's app opens the case

EXPIRY (ISS-003 guard)
-----------------------
QR tokens expire after QR_TOKEN_EXPIRE_HOURS (default: 24h).
Expired tokens return 410 Gone (QRTokenExpiredException).
Single-use: `used = True` after first scan — cannot be reused.

MULTIPLE TOKENS PER CASE
-------------------------
A patient may generate a new QR if the first expires.
Each generation creates a new QRToken row.
Previous unused tokens for the same case are NOT automatically
invalidated — the patient should only show the latest one.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class QRToken(TimestampMixin, Base):
    """
    One single-use QR token for a case.
    """

    __tablename__ = "qr_tokens"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Foreign Key
    # ------------------------------------------------------------------ #
    case_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------ #
    # Token
    # ------------------------------------------------------------------ #
    token: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="URL-safe random token embedded in the QR image. "
                "Generated with secrets.token_urlsafe(32).",
    )

    # ------------------------------------------------------------------ #
    # Expiry & Usage
    # ------------------------------------------------------------------ #
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Token expires at this UTC datetime (default: now + 24h)",
    )
    used: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="True after the doctor has scanned this token (single-use)",
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the doctor scanned the QR",
    )

    # ------------------------------------------------------------------ #
    # Relationship
    # ------------------------------------------------------------------ #
    case: Mapped["Case"] = relationship(  # noqa: F821
        "Case",
        back_populates="qr_tokens",
    )

    def __repr__(self) -> str:
        return (
            f"<QRToken case={self.case_id!r} "
            f"used={self.used} "
            f"expires={self.expires_at}>"
        )
