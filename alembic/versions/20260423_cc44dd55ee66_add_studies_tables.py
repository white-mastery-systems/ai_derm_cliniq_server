"""add_studies_tables

Revision ID: cc44dd55ee66
Revises: b8b5ccfc0300
Create Date: 2026-04-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "cc44dd55ee66"
down_revision: Union[str, Sequence[str], None] = "b8b5ccfc0300"
branch_labels = None
depends_on = None

# Fixed UUID for the seeded Trichoscopic Stability Model study
_TRICHO_STUDY_ID = "c1a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6"


def upgrade() -> None:
    op.create_table(
        "studies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "study_submissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "study_id",
            sa.String(36),
            sa.ForeignKey("studies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "doctor_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("gcs_image_path", sa.String(500), nullable=False),
        sa.Column(
            "features",
            sa.Text(),
            nullable=False,
            comment='JSON: {"black_dots":"yes","broken_hairs":"no",...}',
        ),
        sa.Column(
            "stability",
            sa.String(50),
            nullable=False,
            comment="unstable | stable | regrowing",
        ),
        sa.Column(
            "technical_quality",
            sa.String(20),
            nullable=False,
            comment="good | acceptable | poor",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_study_submissions_study_id", "study_submissions", ["study_id"]
    )
    op.create_index(
        "ix_study_submissions_doctor_id", "study_submissions", ["doctor_id"]
    )

    # Seed the Trichoscopic Stability Model study so it appears on doctor home screens
    # without requiring any admin action — same behaviour as the old Streamlit app.
    op.execute(
        f"""
        INSERT INTO studies (id, title, description, is_active, created_at, updated_at)
        VALUES (
            '{_TRICHO_STUDY_ID}',
            'Trichoscopic Stability Model',
            'Clinical labeling of Alopecia Areata dermoscopic imagery. Mark trichoscopic features present in each specimen to help build the AI stability model.',
            true,
            NOW(),
            NOW()
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_study_submissions_doctor_id", table_name="study_submissions")
    op.drop_index("ix_study_submissions_study_id", table_name="study_submissions")
    op.drop_table("study_submissions")
    op.drop_table("studies")
