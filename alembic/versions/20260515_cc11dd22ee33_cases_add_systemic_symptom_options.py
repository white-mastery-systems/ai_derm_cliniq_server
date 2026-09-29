"""cases add systemic_symptom_options

Revision ID: cc11dd22ee33
Revises: bb00cc11dd22
Create Date: 2026-05-15 00:00:00.000000

Adds systemic_symptom_options column to the cases table.
Stores a JSON list of {id, label} symptom options generated from the AI
differential at the end of save_results_task. Used by the Systemic Check
screen instead of a hardcoded list, so options are case-specific.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "cc11dd22ee33"
down_revision = "bb00cc11dd22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "systemic_symptom_options",
            sa.Text(),
            nullable=True,
            comment="JSON list of {id, label} systemic symptom options generated from the AI differential",
        ),
    )


def downgrade() -> None:
    op.drop_column("cases", "systemic_symptom_options")
