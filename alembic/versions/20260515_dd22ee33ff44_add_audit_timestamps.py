"""add audit timestamps to cases and users

Revision ID: dd22ee33ff44
Revises: cc11dd22ee33
Create Date: 2026-05-15 00:00:00.000000

Adds dedicated audit timestamp columns to capture the exact moment each
critical lifecycle event occurs. These are separate from updated_at so
that the history is never overwritten by later changes.

cases:
  assigned_at               — when a doctor was assigned
  ai_completed_at           — when the Gemini analysis finished
  clinical_status_changed_at — when the doctor last changed clinical_status
  red_flagged_at            — when a red flag was detected
  deleted_at                — when the case was soft-deleted

users:
  approved_at               — when an admin approved a doctor account
  rejected_at               — when an admin rejected a doctor account
  verified_at               — when the user verified their email
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "dd22ee33ff44"
down_revision = "cc11dd22ee33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- cases audit timestamps ---
    op.add_column(
        "cases",
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp when doctor_id was first assigned",
        ),
    )
    op.add_column(
        "cases",
        sa.Column(
            "ai_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp when Gemini AI analysis completed (ai_status=COMPLETED)",
        ),
    )
    op.add_column(
        "cases",
        sa.Column(
            "clinical_status_changed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp of the most recent clinical_status change by a doctor",
        ),
    )
    op.add_column(
        "cases",
        sa.Column(
            "red_flagged_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp when a red flag was detected by the red_flag_check_task",
        ),
    )
    op.add_column(
        "cases",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp when the case was soft-deleted (is_deleted=True)",
        ),
    )

    # --- users audit timestamps ---
    op.add_column(
        "users",
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp when admin approved the doctor account (is_active=True)",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp when admin rejected the doctor account (is_active=False after review)",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp when the user verified their email address",
        ),
    )


def downgrade() -> None:
    op.drop_column("cases", "assigned_at")
    op.drop_column("cases", "ai_completed_at")
    op.drop_column("cases", "clinical_status_changed_at")
    op.drop_column("cases", "red_flagged_at")
    op.drop_column("cases", "deleted_at")

    op.drop_column("users", "approved_at")
    op.drop_column("users", "rejected_at")
    op.drop_column("users", "verified_at")
