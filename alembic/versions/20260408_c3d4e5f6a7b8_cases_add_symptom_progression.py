"""cases_add_symptom_progression

Adds symptom_progression to cases for follow-up consultations.

Shown as "Follow-up status: Better / Same / Worse" on the Case Summary screen
(Screen 4 of the patient flow). Only meaningful when consultation_type = follow_up.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-08 00:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "symptom_progression",
            sa.String(length=50),
            nullable=True,
            comment="Patient-reported symptom change for follow-up cases: better | same | worse",
        ),
    )


def downgrade() -> None:
    op.drop_column("cases", "symptom_progression")
