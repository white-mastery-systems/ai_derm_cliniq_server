"""add_specimen_code_to_submissions

Revision ID: dd55ee66ff77
Revises: cc44dd55ee66
Create Date: 2026-04-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "dd55ee66ff77"
down_revision: Union[str, Sequence[str], None] = "cc44dd55ee66"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "study_submissions",
        sa.Column(
            "specimen_code",
            sa.String(50),
            nullable=True,
            comment="Human-readable specimen label, e.g. AA-2941-B",
        ),
    )


def downgrade() -> None:
    op.drop_column("study_submissions", "specimen_code")
