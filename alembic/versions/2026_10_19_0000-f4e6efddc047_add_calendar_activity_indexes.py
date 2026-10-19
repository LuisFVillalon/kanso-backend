"""add indexes for the calendar activity range

Revision ID: f4e6efddc047
Revises: e130dd555091
Create Date: 2026-10-19 00:00:00.000000

GET /get-calendar-activity reads a user's tasks by due date and by completion
time, and their note sessions by end time, over ranges up to a year. Tasks
already record completion time in completed_date, so only indexes are added.
The task indexes cover active rows only, matching the archived_at filter.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4e6efddc047'
down_revision: Union[str, Sequence[str], None] = 'e130dd555091'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column in ('due_date', 'completed_date'):
        op.create_index(
            f'ix_tasks_user_id_{column}_active',
            'tasks',
            ['user_id', column],
            postgresql_where=sa.text('archived_at IS NULL'),
        )
    op.create_index('ix_note_sessions_user_id_ended_at', 'note_sessions', ['user_id', 'ended_at'])


def downgrade() -> None:
    op.drop_index('ix_note_sessions_user_id_ended_at', table_name='note_sessions')
    for column in ('due_date', 'completed_date'):
        op.drop_index(f'ix_tasks_user_id_{column}_active', table_name='tasks')
