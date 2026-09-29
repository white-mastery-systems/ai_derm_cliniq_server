"""cases_add_title_tags_original

Adds three columns to the cases table derived from the patient home screen
Figma analysis (screen-by-screen audit 2026-04-08):

  1. case_title       — most probable diagnosis name, set by save_results_task
                        when AI analysis completes. Shown as the card title
                        ("Eczema on hands", "Acne Flare-up") in case list views.

  2. symptom_tags     — JSON array of short keyword strings parsed from
                        key_supporting_features in the diagnosis JSON
                        (e.g. '["Redness","Itching","Dry skin"]').
                        Shown as chip tags below the card title.

  3. original_case_id — FK → cases.id for follow-up consultations.
                        NULL for new complaints; set at case creation when
                        consultation_type = follow_up.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-08 00:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1 — case_title
    op.add_column(
        "cases",
        sa.Column(
            "case_title",
            sa.String(length=255),
            nullable=True,
            comment="Most probable AI diagnosis — shown as card title in case list",
        ),
    )

    # 2 — symptom_tags
    op.add_column(
        "cases",
        sa.Column(
            "symptom_tags",
            sa.Text(),
            nullable=True,
            comment="JSON array of short symptom keyword strings for card chips",
        ),
    )

    # 3 — original_case_id
    op.add_column(
        "cases",
        sa.Column(
            "original_case_id",
            sa.String(length=36),
            nullable=True,
            comment="FK to the original case for follow-up consultations",
        ),
    )
    op.create_foreign_key(
        "fk_cases_original_case_id",
        "cases",
        "cases",
        ["original_case_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_cases_original_case_id", "cases", ["original_case_id"])


def downgrade() -> None:
    op.drop_index("ix_cases_original_case_id", table_name="cases")
    op.drop_constraint("fk_cases_original_case_id", "cases", type_="foreignkey")
    op.drop_column("cases", "original_case_id")
    op.drop_column("cases", "symptom_tags")
    op.drop_column("cases", "case_title")
