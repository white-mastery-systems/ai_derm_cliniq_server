"""cases: add case_type column

Revision ID: dd88ee99ff00
Revises: cc77dd88ee99
Create Date: 2026-05-22 00:00:00.000000

Adds a nullable case_type column to the cases table.
Set to 'diagnose' for all cases created via POST /cases/doctor.
NULL for all patient-created cases.
"""

import sqlalchemy as sa
from alembic import op

revision = "dd88ee99ff00"
down_revision = "cc77dd88ee99"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("case_type", sa.String(50), nullable=True, comment="diagnose | null"),
    )


def downgrade() -> None:
    op.drop_column("cases", "case_type")
