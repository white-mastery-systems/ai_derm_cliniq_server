"""cases add patient_treatment_plan

Revision ID: bb00cc11dd22
Revises: aa99bb88cc77
Create Date: 2026-05-08 00:00:00.000000

Adds patient_treatment_plan column to the cases table.
Stores the JSON treatment plan generated on first patient request so
subsequent calls return the cached result without re-calling Gemini.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "bb00cc11dd22"
down_revision = "aa99bb88cc77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "patient_treatment_plan",
            sa.Text(),
            nullable=True,
            comment="JSON: patient-friendly treatment plan, cached after first generation",
        ),
    )


def downgrade() -> None:
    op.drop_column("cases", "patient_treatment_plan")
