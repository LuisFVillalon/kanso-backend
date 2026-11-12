"""add hidden_mood_tags

Revision ID: b5d9e3f7a2c4
Revises: f4c8e2a6b9d1
Create Date: 2026-11-11 00:01:00.000000

hidden_mood_tags holds the built-in mood tags each user has deleted or
renamed, so GET /get-mood-tags leaves them out of the picker. Custom tags need
no row: they come from the user's check-ins. Row Level Security is enabled on
the new table, matching every other public table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5d9e3f7a2c4'
down_revision: Union[str, Sequence[str], None] = 'f4c8e2a6b9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'hidden_mood_tags',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('tag', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('user_id', 'tag', name='uq_hidden_mood_tags_user_tag'),
    )
    op.execute('ALTER TABLE public.hidden_mood_tags ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table('hidden_mood_tags')
