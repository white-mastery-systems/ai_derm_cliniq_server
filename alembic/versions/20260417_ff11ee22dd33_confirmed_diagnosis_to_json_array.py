"""confirmed_diagnosis_to_json_array

Changes doctor_reviews.confirmed_diagnosis from VARCHAR(500) to TEXT
so it can store a JSON array of multiple confirmed diagnoses.

Existing plain-string rows are wrapped into a single-element JSON array
during upgrade so no data is lost.

Revision ID: ff11ee22dd33
Revises: f1a2b3c4d5e6
Create Date: 2026-04-17 00:00:00.000000+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ff11ee22dd33'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Change column type to TEXT
    op.alter_column(
        'doctor_reviews',
        'confirmed_diagnosis',
        existing_type=sa.String(500),
        type_=sa.Text(),
        existing_nullable=True,
    )

    # 2. Wrap any existing plain-string values into a single-element JSON array
    #    so all rows are in the new format: ["Acne Vulgaris"]
    op.execute("""
        UPDATE doctor_reviews
        SET confirmed_diagnosis = json_build_array(confirmed_diagnosis)::text
        WHERE confirmed_diagnosis IS NOT NULL
          AND confirmed_diagnosis NOT LIKE '[%'
    """)


def downgrade() -> None:
    # Unwrap single-element arrays back to plain strings
    op.execute("""
        UPDATE doctor_reviews
        SET confirmed_diagnosis = confirmed_diagnosis::json->>0
        WHERE confirmed_diagnosis IS NOT NULL
          AND confirmed_diagnosis LIKE '[%'
    """)

    op.alter_column(
        'doctor_reviews',
        'confirmed_diagnosis',
        existing_type=sa.Text(),
        type_=sa.String(500),
        existing_nullable=True,
    )
