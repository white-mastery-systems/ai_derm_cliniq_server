"""add diagnosis_type to doctor_reviews

Revision ID: aa11bb22cc33
Revises: ff11ee22dd33
Create Date: 2026-04-19

"""
from alembic import op
import sqlalchemy as sa

revision = 'aa11bb22cc33'
down_revision = 'ff11ee22dd33'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'doctor_reviews',
        sa.Column(
            'diagnosis_type',
            sa.String(50),
            nullable=True,
            comment='clinical | histological | dermoscopic',
        ),
    )


def downgrade() -> None:
    op.drop_column('doctor_reviews', 'diagnosis_type')
