"""add work blocks and habits.planned_time

Revision ID: f4c8e2a6b9d1
Revises: e3b7a9c1d5f2
Create Date: 2026-11-11 00:00:00.000000

work_blocks holds the "work on it" times a user plans on the calendar for a
task or a habit (exactly one of the two), deleted with that task or habit.
habits.planned_time is a habit's usual local time of day ("HH:MM"), or NULL.
Row Level Security is enabled on the new table, matching every other public
table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4c8e2a6b9d1'
down_revision: Union[str, Sequence[str], None] = 'e3b7a9c1d5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'work_blocks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('tasks.id', ondelete='CASCADE'), nullable=True),
        sa.Column('habit_id', sa.Integer(), sa.ForeignKey('habits.id', ondelete='CASCADE'), nullable=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('time_zone', sa.String(length=64), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_date', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('(task_id IS NULL) <> (habit_id IS NULL)', name='ck_work_blocks_task_xor_habit'),
    )
    op.create_index('ix_work_blocks_user_id_starts_at', 'work_blocks', ['user_id', 'starts_at'])
    op.create_index(
        'ix_work_blocks_task_id', 'work_blocks', ['task_id'], postgresql_where=sa.text('task_id IS NOT NULL')
    )
    op.create_index(
        'ix_work_blocks_habit_id', 'work_blocks', ['habit_id'], postgresql_where=sa.text('habit_id IS NOT NULL')
    )
    op.execute('ALTER TABLE public.work_blocks ENABLE ROW LEVEL SECURITY')

    op.add_column('habits', sa.Column('planned_time', sa.Time(), nullable=True))


def downgrade() -> None:
    op.drop_column('habits', 'planned_time')
    op.drop_index('ix_work_blocks_habit_id', table_name='work_blocks')
    op.drop_index('ix_work_blocks_task_id', table_name='work_blocks')
    op.drop_index('ix_work_blocks_user_id_starts_at', table_name='work_blocks')
    op.drop_table('work_blocks')
