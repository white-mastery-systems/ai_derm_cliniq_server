"""add case_audit_logs table

Revision ID: ee33ff44aa55
Revises: dd22ee33ff44
Create Date: 2026-05-15 00:00:00.000000

Creates the case_audit_logs table — an immutable, append-only record of
significant events in a case's lifecycle. Closes GitHub issue #153:
"[MH3] Verify escalation event is logged in audit trail".

The first event type written here is red_flag_triggered, inserted by the
red_flag_check_task Celery worker whenever the AI detects urgent symptoms
and sets a case's red_flag_status to FLAGGED.

actor_id is nullable so that system-triggered events (Celery / AI) can be
recorded without a user actor. actor_role = "system" identifies these rows.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM

# revision identifiers
revision = "ee33ff44aa55"
down_revision = "dd22ee33ff44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the audit_event_type_enum PostgreSQL type (idempotent via DO block)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE audit_event_type_enum AS ENUM ('red_flag_triggered');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    op.create_table(
        "case_audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "case_id",
            sa.String(36),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "event_type",
            PG_ENUM(name="audit_event_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_role", sa.String(20), nullable=False),
        sa.Column("event_data", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Indexes are created by index=True on each column above — no explicit calls needed.


def downgrade() -> None:
    op.drop_table("case_audit_logs")
    op.execute("DROP TYPE IF EXISTS audit_event_type_enum")
