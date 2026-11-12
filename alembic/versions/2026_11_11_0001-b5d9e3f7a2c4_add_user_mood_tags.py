"""add user_mood_tags

Revision ID: b5d9e3f7a2c4
Revises: f4c8e2a6b9d1
Create Date: 2026-11-11 00:01:00.000000

user_mood_tags holds each user's own changes to the mood-tag picker: kind
"hidden" for a built-in tag they deleted or renamed, which GET /get-mood-tags
leaves out, and kind "custom" for a name they renamed a tag to, which it lists
even before a check-in uses it. Row Level Security is enabled on the new
table, matching every other public table.
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
        'user_mood_tags',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=6), nullable=False),
        sa.Column('tag', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('user_id', 'kind', 'tag', name='uq_user_mood_tags_user_kind_tag'),
        sa.CheckConstraint("kind IN ('hidden', 'custom')", name='ck_user_mood_tags_kind'),
    )
    op.execute('ALTER TABLE public.user_mood_tags ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table('user_mood_tags')
