"""add mood check-ins and the profile's mood check-in settings

Revision ID: 766e91c81956
Revises: e1fcef656808
Create Date: 2026-10-20 00:01:00.000000

One mood_checkins row per user, period kind (day, week or month) and period
start (the local day, the Monday of a week, the 1st of a month), so saving a
check-in is an upsert on that key. Mood tags live in the row's own text
array, apart from the task/note/habit tags table. profiles.mood_checkins
holds only the check-in settings the user has changed; NULL means every
default. Row Level Security is enabled on the new table, matching the
lockdown of every other public table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '766e91c81956'
down_revision: Union[str, Sequence[str], None] = 'e1fcef656808'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('profiles', sa.Column('mood_checkins', sa.JSON(), nullable=True))
    op.create_table(
        'mood_checkins',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('period_kind', sa.String(length=5), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('mood', sa.SmallInteger(), nullable=False),
        sa.Column('energy', sa.SmallInteger(), nullable=True),
        sa.Column('stress', sa.SmallInteger(), nullable=True),
        sa.Column('workload', sa.SmallInteger(), nullable=True),
        sa.Column('highlight', sa.String(length=120), nullable=True),
        sa.Column('progress', sa.SmallInteger(), nullable=True),
        sa.Column('word', sa.String(length=24), nullable=True),
        sa.Column('note', sa.String(length=280), nullable=True),
        sa.Column('tags', postgresql.ARRAY(sa.String(length=50)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('user_id', 'period_kind', 'period_start', name='uq_mood_checkins_user_kind_start'),
        sa.CheckConstraint("period_kind IN ('day', 'week', 'month')", name='ck_mood_checkins_period_kind'),
        sa.CheckConstraint('mood BETWEEN 1 AND 5', name='ck_mood_checkins_mood'),
        sa.CheckConstraint('energy BETWEEN 1 AND 5', name='ck_mood_checkins_energy'),
        sa.CheckConstraint('stress BETWEEN 1 AND 5', name='ck_mood_checkins_stress'),
        sa.CheckConstraint('workload BETWEEN 0 AND 2', name='ck_mood_checkins_workload'),
        sa.CheckConstraint('progress BETWEEN 1 AND 5', name='ck_mood_checkins_progress'),
    )
    op.execute('ALTER TABLE public.mood_checkins ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table('mood_checkins')
    op.drop_column('profiles', 'mood_checkins')
