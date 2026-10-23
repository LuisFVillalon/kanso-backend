"""add archived terms

Revision ID: b3e8c1d9f2a7
Revises: aaba43710aa6
Create Date: 2026-10-22 00:04:00.000000

A user can archive their term tracker, so the month calendar keeps showing
it after it ends. Archiving moves the current term (calendar_settings) into
a new archived_terms row; a user can have any number of them, and not
archiving leaves the current term as it is. Row Level Security is enabled
on the new table, matching the lockdown of every other public table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3e8c1d9f2a7'
down_revision: Union[str, Sequence[str], None] = 'aaba43710aa6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'archived_terms',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_archived_terms_user_id', 'archived_terms', ['user_id'])
    op.execute('ALTER TABLE public.archived_terms ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_index('ix_archived_terms_user_id', table_name='archived_terms')
    op.drop_table('archived_terms')
