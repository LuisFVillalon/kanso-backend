"""add events: calendar event series, their tags and per-occurrence exceptions

Revision ID: 239819931369
Revises: d5a0e3f1b4c9
Create Date: 2026-10-23 00:02:00.000000

An event series is a one-off event or one repeating weekly on chosen
weekdays every N weeks, at local times in its own time zone; occurrences
are expanded on read. event_exceptions holds the one occurrence an edit,
archive or delete of "this event" changed. Tags link through event_tags,
and an edited occurrence's own tags through event_exception_tags. Row Level
Security is enabled on the new tables, matching the lockdown of every other
public table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '239819931369'
down_revision: Union[str, Sequence[str], None] = 'd5a0e3f1b4c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('events', 'event_tags', 'event_exceptions', 'event_exception_tags')


def upgrade() -> None:
    op.create_table(
        'events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('all_day', sa.Boolean(), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('start_time', sa.Time(), nullable=True),
        sa.Column('end_time', sa.Time(), nullable=True),
        sa.Column('time_zone', sa.String(length=64), nullable=False),
        sa.Column('repeat_days', sa.JSON(), nullable=False),
        sa.Column('repeat_interval', sa.Integer(), nullable=False),
        sa.Column('reminder_minutes', sa.Integer(), nullable=True),
        sa.Column('split_from_id', sa.Integer(), sa.ForeignKey('events.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_date', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_events_user_id', 'events', ['user_id'])
    op.create_index(
        'ix_events_user_id_live', 'events', ['user_id'], postgresql_where=sa.text('deleted_at IS NULL')
    )
    op.create_index(
        'ix_events_deleted_at', 'events', ['deleted_at'], postgresql_where=sa.text('deleted_at IS NOT NULL')
    )
    op.create_index(
        'ix_events_split_from_id', 'events', ['split_from_id'], postgresql_where=sa.text('split_from_id IS NOT NULL')
    )

    op.create_table(
        'event_tags',
        sa.Column('event_id', sa.Integer(), sa.ForeignKey('events.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('tag_id', sa.Integer(), sa.ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
    )

    op.create_table(
        'event_exceptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('event_id', sa.Integer(), sa.ForeignKey('events.id', ondelete='CASCADE'), nullable=False),
        sa.Column('occurrence_date', sa.Date(), nullable=False),
        sa.Column('date', sa.Date(), nullable=True),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('all_day', sa.Boolean(), nullable=True),
        sa.Column('start_time', sa.Time(), nullable=True),
        sa.Column('end_time', sa.Time(), nullable=True),
        sa.Column('tags_overridden', sa.Boolean(), nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('event_id', 'occurrence_date', name='uq_event_exceptions_event_occurrence'),
    )
    op.create_index(
        'ix_event_exceptions_deleted_at',
        'event_exceptions',
        ['deleted_at'],
        postgresql_where=sa.text('deleted_at IS NOT NULL'),
    )

    op.create_table(
        'event_exception_tags',
        sa.Column(
            'exception_id', sa.Integer(), sa.ForeignKey('event_exceptions.id', ondelete='CASCADE'), primary_key=True
        ),
        sa.Column('tag_id', sa.Integer(), sa.ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
    )

    for table in TABLES:
        op.execute(f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
