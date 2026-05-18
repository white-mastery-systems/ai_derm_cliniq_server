"""audit_log: add doctor intervention event types

Revision ID: cc77dd88ee99
Revises: bb66cc77dd88
Create Date: 2026-05-17 00:00:00.000000

Extends audit_event_type_enum with three doctor intervention events:
  review_submitted    — doctor sets review_status=completed
  doctor_assigned     — doctor auto-assigned via QR scan or display-ID lookup
  diagnosis_confirmed — doctor sets or updates confirmed_diagnosis on a review

Closes GitHub issue #173: "[MH8] Verify doctor intervention events are
captured separately from AI events".
"""

from alembic import op

revision = "cc77dd88ee99"
down_revision = "bb66cc77dd88"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'review_submitted';
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'doctor_assigned';
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'diagnosis_confirmed';
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)


def downgrade() -> None:
    pass
