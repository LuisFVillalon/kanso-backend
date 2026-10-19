"""add archived_at to tasks, notes and tags

Revision ID: e130dd555091
Revises: 782afe89b927
Create Date: 2026-10-18 00:00:00.000000

Archiving hides a row without deleting it: archived_at is set when the row is
archived and NULL while it is active. List endpoints return only active rows
by default, so each table gets a partial index on user_id covering just those.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e130dd555091'
down_revision: Union[str, Sequence[str], None] = '782afe89b927'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('tasks', 'notes', 'tags')


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
        op.create_index(
            f'ix_{table}_user_id_active',
            table,
            ['user_id'],
            postgresql_where=sa.text('archived_at IS NULL'),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f'ix_{table}_user_id_active', table_name=table)
        op.drop_column(table, 'archived_at')
