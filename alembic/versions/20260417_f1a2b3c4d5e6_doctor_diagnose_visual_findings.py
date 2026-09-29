"""doctor_diagnose_visual_findings

Adds Case.visual_findings column and extends image_type_enum with
'clinical' and 'pathology' values for the doctor diagnose flow.

Revision ID: f1a2b3c4d5e6
Revises: ac43a2a21a19
Create Date: 2026-04-17 00:00:00.000000+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = '6f7f5f8c1465'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new image type values to the existing enum.
    # PostgreSQL enum values cannot be removed, so downgrade leaves them.
    # IF NOT EXISTS makes this safe to run multiple times.
    op.execute("ALTER TYPE image_type_enum ADD VALUE IF NOT EXISTS 'clinical'")
    op.execute("ALTER TYPE image_type_enum ADD VALUE IF NOT EXISTS 'pathology'")

    # Add visual_findings column to cases table (nullable — existing rows get NULL)
    op.add_column(
        'cases',
        sa.Column(
            'visual_findings',
            sa.Text(),
            nullable=True,
            comment=(
                'JSON: {clinical:{...}, dermoscopy:{...}, pathology:{...}} — '
                'populated by generate_visual_findings_task'
            ),
        ),
    )


def downgrade() -> None:
    # Drop the column (enum values cannot be removed from PostgreSQL enums)
    op.drop_column('cases', 'visual_findings')
