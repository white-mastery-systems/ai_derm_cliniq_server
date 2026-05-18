"""audit_log: add note_edited event type

Revision ID: aa55bb66cc77
Revises: ff44aa55bb66
Create Date: 2026-05-16 00:00:00.000000

Extends audit_event_type_enum with:
  note_edited — written every time a doctor edits review_notes on a
  DoctorReview. event_data captures old_note and new_note so the full
  edit history is preserved immutably.

Closes GitHub issue #165: "[MH6] Verify notes are editable and changes
are tracked".
"""

from alembic import op

revision = "aa55bb66cc77"
down_revision = "ff44aa55bb66"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'note_edited';
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)


def downgrade() -> None:
    pass
