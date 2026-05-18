"""audit_log: add case_accessed event type

Revision ID: bb66cc77dd88
Revises: aa55bb66cc77
Create Date: 2026-05-17 00:00:00.000000

Extends audit_event_type_enum with:
  case_accessed — written every time an admin fetches full case detail
  via GET /admin/cases/{case_id}. Provides a traceable record of which
  admin accessed which patient case and when.

Closes GitHub issue #195: "[MH14] Verify audit logs capture prompt edits,
AI setting changes, doctor approvals, and case access".
"""

from alembic import op

revision = "bb66cc77dd88"
down_revision = "aa55bb66cc77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'case_accessed';
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)


def downgrade() -> None:
    pass
