"""add term rollovers: a queued next term and auto-archive

Revision ID: d5a0e3f1b4c9
Revises: c4f9d2e0a3b8
Create Date: 2026-10-23 00:01:00.000000

One row per user. A user can queue the next term (next_*) while a current
term (calendar_settings) exists; once the current term ends it becomes the
current one. auto_archive (default off) archives the ended term into
archived_terms first; with it off the ended term is kept in ended_* until the
user archives or dismisses it. Row Level Security is enabled on the new
table, matching the lockdown of every other public table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5a0e3f1b4c9'
down_revision: Union[str, Sequence[str], None] = 'c4f9d2e0a3b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'term_rollovers',
        sa.Column('user_id', sa.String(length=36), primary_key=True),
        sa.Column('auto_archive', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('next_title', sa.String(), nullable=True),
        sa.Column('next_start_date', sa.Date(), nullable=True),
        sa.Column('next_end_date', sa.Date(), nullable=True),
        sa.Column('ended_title', sa.String(), nullable=True),
        sa.Column('ended_start_date', sa.Date(), nullable=True),
        sa.Column('ended_end_date', sa.Date(), nullable=True),
    )
    op.execute('ALTER TABLE public.term_rollovers ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table('term_rollovers')
