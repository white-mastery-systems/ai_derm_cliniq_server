"""fix_enum_case_add_verification_tokens

Two changes in one migration:

1. FIX ENUM CASE (all existing enums → lowercase values)
   The initial migration created 8 enum types with UPPERCASE values
   ('PENDING', 'ACTIVE', 'NEW_COMPLAINT', etc.).  The Python models
   use lowercase .value attributes ('pending', 'active', 'new_complaint').
   SQLAlchemy was accidentally matching by sending the enum .name (uppercase)
   instead of .value.  Now that values_callable is set on every enum column,
   the ORM sends lowercase — so the DB types must also store lowercase.

   For each affected enum:
     a. ALTER the column to TEXT
     b. UPDATE existing rows to LOWER(column_value)
     c. DROP the old uppercase enum type
     d. CREATE the new lowercase enum type
     e. ALTER the column back to the new enum type

2. CREATE verification_tokens TABLE
   The VerificationToken model existed in code but was never migrated.
   Any call to POST /auth/forgot-password or POST /auth/verify-email
   would crash with "relation verification_tokens does not exist".
   Creates the table and token_purpose_enum (lowercase from the start).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-09 12:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_token_purpose_enum = sa.Enum(
    "password_reset", "email_verify",
    name="token_purpose_enum",
)


# ---------------------------------------------------------------------------
# Helper: recreate a PostgreSQL enum type with new values
# ---------------------------------------------------------------------------
def _recreate_enum(
    table: str,
    column: str,
    old_type_name: str,
    new_values: list[str],
) -> None:
    """
    Convert column → TEXT, lowercase existing data, drop old enum,
    create new enum, convert column back.
    """
    new_type_name = old_type_name  # same name, different values
    tmp_type_name = f"{old_type_name}_new"

    # 1. Cast column to plain text
    op.execute(f'ALTER TABLE {table} ALTER COLUMN {column} TYPE text')

    # 2. Lowercase existing rows
    op.execute(f'UPDATE {table} SET {column} = LOWER({column})')

    # 3. Drop the old uppercase enum type
    op.execute(f'DROP TYPE IF EXISTS {old_type_name}')

    # 4. Create new lowercase enum type (use tmp name to avoid collision)
    values_sql = ", ".join(f"'{v}'" for v in new_values)
    op.execute(f"CREATE TYPE {tmp_type_name} AS ENUM ({values_sql})")

    # 5. Cast column to new enum type
    op.execute(
        f'ALTER TABLE {table} ALTER COLUMN {column} '
        f'TYPE {tmp_type_name} USING {column}::{tmp_type_name}'
    )

    # 6. Rename to final name
    op.execute(f'ALTER TYPE {tmp_type_name} RENAME TO {new_type_name}')


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 1. Fix enum case — convert all uppercase DB enums to lowercase
    # ------------------------------------------------------------------ #

    _recreate_enum(
        table="users",
        column="role",
        old_type_name="user_role_enum",
        new_values=["patient", "doctor", "admin"],
    )

    _recreate_enum(
        table="cases",
        column="consultation_type",
        old_type_name="consultation_type_enum",
        new_values=["new_complaint", "follow_up"],
    )

    _recreate_enum(
        table="cases",
        column="ai_status",
        old_type_name="ai_status_enum",
        new_values=["pending", "processing", "completed", "failed"],
    )

    _recreate_enum(
        table="cases",
        column="clinical_status",
        old_type_name="clinical_status_enum",
        new_values=["active", "follow_up_available", "monitoring", "resolved"],
    )

    _recreate_enum(
        table="case_images",
        column="image_type",
        old_type_name="image_type_enum",
        new_values=["skin", "prescription", "dermoscopy"],
    )

    _recreate_enum(
        table="case_reports",
        column="report_type",
        old_type_name="report_type_enum",
        new_values=["doctor", "patient"],
    )

    _recreate_enum(
        table="doctor_reviews",
        column="review_status",
        old_type_name="review_status_enum",
        new_values=["pending", "in_progress", "completed"],
    )

    _recreate_enum(
        table="messages",
        column="role",
        old_type_name="message_role_enum",
        new_values=["ai", "patient", "doctor"],
    )

    # ------------------------------------------------------------------ #
    # 2. Create verification_tokens table
    # ------------------------------------------------------------------ #
    # Use DO block so this is idempotent — won't fail if type already exists
    # (can happen when a previous migration run created the type but crashed
    # before creating the table).
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE token_purpose_enum AS ENUM ('password_reset', 'email_verify');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)

    op.create_table(
        "verification_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "token_hash",
            sa.String(64),
            nullable=False,
            unique=True,
            index=True,
            comment="SHA-256 hex digest of the raw token. Never store the raw token.",
        ),
        sa.Column(
            "purpose",
            PgEnum("password_reset", "email_verify", name="token_purpose_enum", create_type=False),
            nullable=False,
            comment="password_reset | email_verify",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    # Drop verification_tokens
    op.drop_table("verification_tokens")
    _token_purpose_enum.drop(op.get_bind(), checkfirst=True)

    # Restore enums to uppercase
    # (reverse order, same helper — uppercase values)
    _recreate_enum(
        table="messages",
        column="role",
        old_type_name="message_role_enum",
        new_values=["AI", "PATIENT", "DOCTOR"],
    )
    _recreate_enum(
        table="doctor_reviews",
        column="review_status",
        old_type_name="review_status_enum",
        new_values=["PENDING", "IN_PROGRESS", "COMPLETED"],
    )
    _recreate_enum(
        table="case_reports",
        column="report_type",
        old_type_name="report_type_enum",
        new_values=["DOCTOR", "PATIENT"],
    )
    _recreate_enum(
        table="case_images",
        column="image_type",
        old_type_name="image_type_enum",
        new_values=["SKIN", "PRESCRIPTION", "DERMOSCOPY"],
    )
    _recreate_enum(
        table="cases",
        column="clinical_status",
        old_type_name="clinical_status_enum",
        new_values=["ACTIVE", "FOLLOW_UP_AVAILABLE", "MONITORING", "RESOLVED"],
    )
    _recreate_enum(
        table="cases",
        column="ai_status",
        old_type_name="ai_status_enum",
        new_values=["PENDING", "PROCESSING", "COMPLETED", "FAILED"],
    )
    _recreate_enum(
        table="cases",
        column="consultation_type",
        old_type_name="consultation_type_enum",
        new_values=["NEW_COMPLAINT", "FOLLOW_UP"],
    )
    _recreate_enum(
        table="users",
        column="role",
        old_type_name="user_role_enum",
        new_values=["PATIENT", "DOCTOR", "ADMIN"],
    )
