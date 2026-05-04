"""add fcm_token to users

Revision ID: aa99bb88cc77
Revises: ff77aa88bb99
Create Date: 2026-05-02 00:00:00.000000

Adds fcm_token column to the users table for Firebase push notification delivery.
NULL = user has not granted notification permission / token not yet captured.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "aa99bb88cc77"
down_revision = "ff77aa88bb99"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "fcm_token",
            sa.String(512),
            nullable=True,
            comment="Firebase Cloud Messaging token — updated on every login.",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "fcm_token")
