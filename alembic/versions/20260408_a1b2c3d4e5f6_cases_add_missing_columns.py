"""cases_add_missing_columns

Adds columns that exist in the ORM model but were absent from the initial
migration:

  1. Rename consent_given → consent_ai_analysis
  2. Rename consent_given_at → consent_ai_analysis_at
  3. Add consent_research       (Boolean, default False)
  4. Add body_location          (VARCHAR 100, nullable)
  5. Add red_flag_status        (Enum, default 'not_checked')
  6. Add red_flags              (Text, nullable)
  7. Add red_flag_advice        (Text, nullable)
  8. Create sequence case_number_seq starting at 9001
  9. Add case_number            (Integer, nullable, unique, default nextval)

Revision ID: a1b2c3d4e5f6
Revises: 7d9c393d2876
Create Date: 2026-04-08 00:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "7d9c393d2876"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_red_flag_status_enum = sa.Enum(
    "not_checked", "checking", "clear", "flagged",
    name="red_flag_status_enum",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1 & 2 — rename consent columns
    # ------------------------------------------------------------------
    op.alter_column("cases", "consent_given", new_column_name="consent_ai_analysis")
    op.alter_column("cases", "consent_given_at", new_column_name="consent_ai_analysis_at")

    # ------------------------------------------------------------------
    # 3 — consent_research
    # ------------------------------------------------------------------
    op.add_column(
        "cases",
        sa.Column(
            "consent_research",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Optional. Patient allows anonymized data for academic research.",
        ),
    )

    # ------------------------------------------------------------------
    # 4 — body_location
    # ------------------------------------------------------------------
    op.add_column(
        "cases",
        sa.Column(
            "body_location",
            sa.String(length=100),
            nullable=True,
            comment="Body area of the lesion e.g. face, hand, back, arm, leg, neck, chest, other",
        ),
    )

    # ------------------------------------------------------------------
    # 5, 6, 7 — red flag columns
    # ------------------------------------------------------------------
    _red_flag_status_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "cases",
        sa.Column(
            "red_flag_status",
            _red_flag_status_enum,
            nullable=False,
            server_default="not_checked",
            comment="Status of the systemic / red flag check",
        ),
    )
    op.add_column(
        "cases",
        sa.Column(
            "red_flags",
            sa.Text(),
            nullable=True,
            comment="JSON list of flagged conditions detected by the AI red flag check",
        ),
    )
    op.add_column(
        "cases",
        sa.Column(
            "red_flag_advice",
            sa.Text(),
            nullable=True,
            comment="AI-generated advice shown to patient when red flags are detected",
        ),
    )

    # ------------------------------------------------------------------
    # 8 & 9 — case_number sequence + column
    # ------------------------------------------------------------------
    op.execute("CREATE SEQUENCE IF NOT EXISTS case_number_seq START 9001")

    op.add_column(
        "cases",
        sa.Column(
            "case_number",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("nextval('case_number_seq')"),
            comment="Sequential display number, shown as AI-{n} in the UI",
        ),
    )
    op.create_index("ix_cases_case_number", "cases", ["case_number"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_cases_case_number", table_name="cases")
    op.drop_column("cases", "case_number")
    op.execute("DROP SEQUENCE IF EXISTS case_number_seq")

    op.drop_column("cases", "red_flag_advice")
    op.drop_column("cases", "red_flags")
    op.drop_column("cases", "red_flag_status")
    _red_flag_status_enum.drop(op.get_bind(), checkfirst=True)

    op.drop_column("cases", "body_location")
    op.drop_column("cases", "consent_research")

    op.alter_column("cases", "consent_ai_analysis_at", new_column_name="consent_given_at")
    op.alter_column("cases", "consent_ai_analysis", new_column_name="consent_given")
