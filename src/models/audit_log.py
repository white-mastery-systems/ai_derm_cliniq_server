"""
models/audit_log.py — Case Audit Log
=====================================
Immutable, append-only record of significant events in a case's lifecycle.

WHY THIS TABLE EXISTS
----------------------
Clinical governance and regulatory compliance require a traceable, tamper-evident
record of critical events — specifically escalations (red-flag triggers) where
the AI flagged a case as high-risk. This closes GitHub issue #153:
"[MH3] Verify escalation event is logged in audit trail".

DESIGN DECISIONS
-----------------
- No TimestampMixin: we only need `created_at`. There is no `updated_at`
  because audit rows are NEVER modified after insertion.
- actor_id is NULL for system-triggered events (Celery / AI worker).
  actor_role = "system" identifies these rows.
- event_data stores a JSON blob with event-specific payload (flags, advice, etc.)
  so each event type can carry its own structured context without schema changes.
- CASCADE on case_id means audit rows are removed if the case is hard-deleted.
  Soft-deleted cases (is_deleted=True) are never hard-deleted, so audit rows
  survive for all normal lifecycle paths.

CURRENT EVENT TYPES
--------------------
  red_flag_triggered   — AI identified urgent symptoms; escalation sent to admins.
  disclaimer_accepted  — Patient explicitly accepted the medical AI disclaimer at
                         case creation. Closes GitHub issue #161.

(Additional events — doctor_assigned, review_submitted — can be added by
appending values to AuditEventType without a breaking schema change, since
PostgreSQL ALTER TYPE ADD VALUE is non-destructive.)
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, new_uuid


class AuditEventType(str, enum.Enum):
    RED_FLAG_TRIGGERED  = "red_flag_triggered"
    DISCLAIMER_ACCEPTED = "disclaimer_accepted"


class CaseAuditLog(Base):
    """
    One immutable audit event tied to a case.

    Rows are inserted by:
      - Celery workers  (actor_id=None, actor_role="system")
      - Future: API routes for human actions (actor_id=user.id, actor_role=user.role)
    """

    __tablename__ = "case_audit_logs"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Case Link
    # ------------------------------------------------------------------ #
    case_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Case this event belongs to",
    )

    # ------------------------------------------------------------------ #
    # Event Classification
    # ------------------------------------------------------------------ #
    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(
            AuditEventType,
            name="audit_event_type_enum",
            create_type=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
        comment="Categorised event type",
    )

    # ------------------------------------------------------------------ #
    # Actor — who or what triggered the event
    # ------------------------------------------------------------------ #
    actor_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who triggered the event. NULL for AI / Celery system events.",
    )
    actor_role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Role at time of event: system | patient | doctor | admin",
    )

    # ------------------------------------------------------------------ #
    # Event Payload
    # ------------------------------------------------------------------ #
    event_data: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON blob with event-specific data (flags list, advice text, etc.)",
    )

    # ------------------------------------------------------------------ #
    # Timestamp — set by server, never changed
    # ------------------------------------------------------------------ #
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="Immutable event timestamp (UTC). Never updated.",
    )

    def __repr__(self) -> str:
        return (
            f"<CaseAuditLog id={self.id!r} "
            f"case_id={self.case_id!r} "
            f"event_type={self.event_type}>"
        )
