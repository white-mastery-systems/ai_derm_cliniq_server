"""audit_log: add clinical_status_changed event type

Revision ID: ee99ff00aa11
Revises: dd88ee99ff00
Create Date: 2026-05-24 00:00:00.000000

Extends audit_event_type_enum with:
  clinical_status_changed — doctor changed the clinical_status on a case via
                            PATCH /review. Captures old_status and new_status
                            in event_data.

Closes GitHub issue #344: "[DTF8] Test status change is logged in case timeline".
"""

from alembic import op

revision = "ee99ff00aa11"
down_revision = "dd88ee99ff00"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'clinical_status_changed';
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)


def downgrade() -> None:
    pass
