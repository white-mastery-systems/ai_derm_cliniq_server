"""add_dependents

Adds the dependents table for the "Someone else" patient selection flow.
Also adds dependent_id FK column to cases.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-09 00:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create dependents table
    op.create_table(
        "dependents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("patient_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("relationship", sa.String(50), nullable=False),
        sa.Column("date_of_birth", sa.Date, nullable=True),
        sa.Column("gender", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_dependents_patient_id", "dependents", ["patient_id"])

    # 2. Add dependent_id FK to cases
    op.add_column(
        "cases",
        sa.Column(
            "dependent_id",
            sa.String(36),
            sa.ForeignKey("dependents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_cases_dependent_id", "cases", ["dependent_id"])


def downgrade() -> None:
    op.drop_index("ix_cases_dependent_id", table_name="cases")
    op.drop_column("cases", "dependent_id")
    op.drop_index("ix_dependents_patient_id", table_name="dependents")
    op.drop_table("dependents")
