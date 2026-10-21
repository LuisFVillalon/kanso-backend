"""add in-app notifications: task reminder lead, preferences and read state

Revision ID: e1fcef656808
Revises: af884f45c2a0
Create Date: 2026-10-20 00:00:00.000000

Notifications are computed on demand from tasks, habits and the profile, so
only three things are stored: each task's reminder lead in minutes
(tasks.reminder_minutes, NULL for the user's default), each user's
notification preferences (per-type overrides, quiet hours, default lead),
and the read/dismissed state of individual notifications, keyed by their
deterministic id. Preferences hold only the types switched off or on, so
new alert types need no backfill. Row Level Security is enabled on both new
tables, matching the lockdown of every other public table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1fcef656808'
down_revision: Union[str, Sequence[str], None] = 'af884f45c2a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_TABLES = ('notification_preferences', 'notification_states')


def upgrade() -> None:
    op.add_column('tasks', sa.Column('reminder_minutes', sa.Integer(), nullable=True))
    op.create_table(
        'notification_preferences',
        sa.Column('user_id', sa.String(length=36), primary_key=True),
        sa.Column('types', sa.JSON(), nullable=True),
        sa.Column('quiet_hours_start', sa.String(length=5), nullable=True),
        sa.Column('quiet_hours_end', sa.String(length=5), nullable=True),
        sa.Column('default_reminder_minutes', sa.Integer(), nullable=True),
    )
    op.create_table(
        'notification_states',
        sa.Column('user_id', sa.String(length=36), primary_key=True),
        sa.Column('notification_id', sa.String(length=100), primary_key=True),
        sa.Column('occurs_on', sa.Date(), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_notification_states_occurs_on', 'notification_states', ['occurs_on'])
    for table in _NEW_TABLES:
        op.execute(f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_index('ix_notification_states_occurs_on', table_name='notification_states')
    op.drop_table('notification_states')
    op.drop_table('notification_preferences')
    op.drop_column('tasks', 'reminder_minutes')
