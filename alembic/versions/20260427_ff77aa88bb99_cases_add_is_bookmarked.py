"""cases_add_is_bookmarked

Revision ID: ff77aa88bb99
Revises: ee66ff77aa88
Create Date: 2026-04-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ff77aa88bb99"
down_revision: Union[str, Sequence[str], None] = "ee66ff77aa88"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "is_bookmarked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Doctor-set flag. True = appears in the Important Cases list.",
        ),
    )
    op.create_index("ix_cases_is_bookmarked", "cases", ["is_bookmarked"])


def downgrade() -> None:
    op.drop_index("ix_cases_is_bookmarked", table_name="cases")
    op.drop_column("cases", "is_bookmarked")
