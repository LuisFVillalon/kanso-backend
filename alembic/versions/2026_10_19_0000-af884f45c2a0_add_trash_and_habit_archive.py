"""add deleted_at (the trash) to tasks, notes, tags and habits, and archived_at to habits

Revision ID: af884f45c2a0
Revises: f4e6efddc047
Create Date: 2026-10-19 00:00:00.000000

Deleting a task, note, tag or habit now sets deleted_at instead of removing
the row, so it can be restored from the trash for 30 days. Habits also gain
the archived_at column tasks, notes and tags already have. Lists return only
active, undeleted rows, so each table's partial index on user_id now covers
just those, and a partial index on deleted_at serves the expiry purge.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'af884f45c2a0'
down_revision: Union[str, Sequence[str], None] = 'f4e6efddc047'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('tasks', 'notes', 'tags', 'habits')
_ARCHIVED_BEFORE = ('tasks', 'notes', 'tags')


def upgrade() -> None:
    op.add_column('habits', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
    for table in _ARCHIVED_BEFORE:
        op.drop_index(f'ix_{table}_user_id_active', table_name=table)
    for table in _TABLES:
        op.add_column(table, sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
        op.create_index(
            f'ix_{table}_user_id_active',
            table,
            ['user_id'],
            postgresql_where=sa.text('archived_at IS NULL AND deleted_at IS NULL'),
        )
        op.create_index(
            f'ix_{table}_deleted_at',
            table,
            ['deleted_at'],
            postgresql_where=sa.text('deleted_at IS NOT NULL'),
        )


def downgrade() -> None:
    # Without deleted_at, rows in the trash would reappear as live, so they
    # are deleted for good first (their tag links, habit logs and note
    # sessions cascade). Live subtasks of a trashed task are detached first.
    op.execute(
        'UPDATE tasks SET parent_task_id = NULL WHERE deleted_at IS NULL AND parent_task_id IN '
        '(SELECT id FROM tasks WHERE deleted_at IS NOT NULL)'
    )
    for table in _TABLES:
        op.execute(f'DELETE FROM {table} WHERE deleted_at IS NOT NULL')
        op.drop_index(f'ix_{table}_deleted_at', table_name=table)
        op.drop_index(f'ix_{table}_user_id_active', table_name=table)
        op.drop_column(table, 'deleted_at')
    for table in _ARCHIVED_BEFORE:
        op.create_index(
            f'ix_{table}_user_id_active',
            table,
            ['user_id'],
            postgresql_where=sa.text('archived_at IS NULL'),
        )
    op.drop_column('habits', 'archived_at')
