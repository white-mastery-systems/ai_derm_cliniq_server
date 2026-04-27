"""cases_add_is_deleted

Revision ID: ee66ff77aa88
Revises: dd55ee66ff77
Create Date: 2026-04-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ee66ff77aa88"
down_revision: Union[str, Sequence[str], None] = "dd55ee66ff77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Soft-delete flag. Deleted cases are hidden from all queries but never removed from the DB.",
        ),
    )
    op.create_index("ix_cases_is_deleted", "cases", ["is_deleted"])


def downgrade() -> None:
    op.drop_index("ix_cases_is_deleted", table_name="cases")
    op.drop_column("cases", "is_deleted")
