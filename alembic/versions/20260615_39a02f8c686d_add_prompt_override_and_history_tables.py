"""add_prompt_override_and_history_tables

Revision ID: 39a02f8c686d
Revises: ee99ff00aa11
Create Date: 2026-06-15 07:44:44.420030+00:00

WHY
---
Admin prompt overrides were previously stored only in Redis.
Redis relies on periodic RDB snapshots — data can be lost between
snapshots if Redis is restarted unexpectedly, or if volumes are wiped.

These two tables make PostgreSQL the durable source of truth:
  prompt_overrides  — current active override per prompt key
  prompt_history    — version history (max 10 entries per key)

Redis is kept as a fast-read cache, warmed from PostgreSQL on startup.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '39a02f8c686d'
down_revision: Union[str, Sequence[str], None] = 'ee99ff00aa11'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'prompt_history',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('key', sa.String(length=100), nullable=False,
                  comment='Prompt key this history entry belongs to'),
        sa.Column('value', sa.Text(), nullable=False,
                  comment='The prompt text that was active before the next write'),
        sa.Column('replaced_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False,
                  comment='When this value was replaced (used for ordering)'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_prompt_history_key', 'prompt_history', ['key'], unique=False)
    op.create_index('ix_prompt_history_replaced_at', 'prompt_history', ['replaced_at'], unique=False)

    op.create_table(
        'prompt_overrides',
        sa.Column('key', sa.String(length=100), nullable=False,
                  comment="Prompt key (e.g. 'patient_follow_up_question')"),
        sa.Column('value', sa.Text(), nullable=False,
                  comment='The custom prompt text set by the admin'),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False,
                  comment='When this override was last written'),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('prompt_overrides')
    op.drop_index('ix_prompt_history_replaced_at', table_name='prompt_history')
    op.drop_index('ix_prompt_history_key', table_name='prompt_history')
    op.drop_table('prompt_history')
