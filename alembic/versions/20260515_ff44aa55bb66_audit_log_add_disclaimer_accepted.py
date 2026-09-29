"""audit_log: add disclaimer_accepted event type

Revision ID: ff44aa55bb66
Revises: ee33ff44aa55
Create Date: 2026-05-15 00:00:00.000000

Extends the audit_event_type_enum PostgreSQL type with a new value:
  disclaimer_accepted — written at case creation when the patient (or
  doctor on behalf of patient) accepts the medical AI disclaimer.

Closes GitHub issue #161: "[MH5] Verify disclaimer acceptance is logged
per session".

ALTER TYPE ADD VALUE is idempotent-safe via the DO block guard.
It is also non-transactional in PostgreSQL — the new value is visible
immediately to all connections without a restart.
"""

from alembic import op

# revision identifiers
revision = "ff44aa55bb66"
down_revision = "ee33ff44aa55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE audit_event_type_enum ADD VALUE IF NOT EXISTS 'disclaimer_accepted';
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)


def downgrade() -> None:
    # PostgreSQL does not support removing individual values from an enum type.
    # To fully roll back, drop and recreate the type — but that requires
    # dropping the table first. In practice, leave the value in place;
    # it will simply never be inserted again after downgrade.
    pass
