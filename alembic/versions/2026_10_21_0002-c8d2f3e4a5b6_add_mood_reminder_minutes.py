"""add notification_preferences.mood_reminder_minutes: the mood reminder's lead

Revision ID: c8d2f3e4a5b6
Revises: b7c1e2d3f4a5
Create Date: 2026-10-21 00:02:00.000000

  mood_reminder_minutes  INTEGER  nullable — 30 | 60 minutes before "Call it a day"; NULL for the default, 60
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8d2f3e4a5b6'
down_revision: Union[str, Sequence[str], None] = 'b7c1e2d3f4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notification_preferences', sa.Column('mood_reminder_minutes', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('notification_preferences', 'mood_reminder_minutes')
