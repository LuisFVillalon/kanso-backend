"""add google calendar: read-only connections, calendars, imported events and their tags

Revision ID: c8d2f4a6b1e3
Revises: a7c3e5f9b1d2
Create Date: 2026-11-09 00:00:00.000000

A connection holds the user's encrypted Google refresh token; each of their
calendars has a selected flag and an optional default tag; imported events
are one row per occurrence, with their own tags alongside. Row Level Security
is enabled on the new tables, matching the lockdown of every other public
table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8d2f4a6b1e3'
down_revision: Union[str, Sequence[str], None] = 'a7c3e5f9b1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    'google_calendar_connections',
    'google_oauth_states',
    'google_calendars',
    'google_calendar_events',
    'google_calendar_event_tags',
)


def upgrade() -> None:
    op.create_table(
        'google_calendar_connections',
        sa.Column('user_id', sa.String(length=36), primary_key=True),
        sa.Column('token_ciphertext', sa.LargeBinary(), nullable=False),
        sa.Column('token_nonce', sa.LargeBinary(), nullable=False),
        sa.Column('key_version', sa.SmallInteger(), nullable=False),
        sa.Column('email', sa.Text(), nullable=True),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        'google_oauth_states',
        sa.Column('user_id', sa.String(length=36), primary_key=True),
        sa.Column('state_hash', sa.String(length=64), nullable=False, unique=True),
        sa.Column('return_origin', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        'google_calendars',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'user_id',
            sa.String(length=36),
            sa.ForeignKey('google_calendar_connections.user_id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('google_calendar_id', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('color', sa.String(length=32), nullable=True),
        sa.Column('time_zone', sa.String(length=64), nullable=False),
        sa.Column('selected', sa.Boolean(), nullable=False),
        sa.Column('default_tag_id', sa.Integer(), sa.ForeignKey('tags.id', ondelete='SET NULL'), nullable=True),
        sa.Column('sync_token', sa.Text(), nullable=True),
        sa.Column('synced_through', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('user_id', 'google_calendar_id', name='uq_google_calendars_user_calendar'),
    )

    op.create_table(
        'google_calendar_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column(
            'calendar_id', sa.Integer(), sa.ForeignKey('google_calendars.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('google_event_id', sa.Text(), nullable=False),
        sa.Column('recurring_event_id', sa.Text(), nullable=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('all_day', sa.Boolean(), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('time_zone', sa.String(length=64), nullable=False),
        sa.Column('html_link', sa.Text(), nullable=True),
        sa.Column('tags_overridden', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('calendar_id', 'google_event_id', name='uq_google_calendar_events_calendar_event'),
    )
    op.create_index('ix_google_calendar_events_user_start', 'google_calendar_events', ['user_id', 'start_date'])

    op.create_table(
        'google_calendar_event_tags',
        sa.Column(
            'event_id', sa.Integer(), sa.ForeignKey('google_calendar_events.id', ondelete='CASCADE'), primary_key=True
        ),
        sa.Column('tag_id', sa.Integer(), sa.ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
    )

    for table in TABLES:
        op.execute(f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in ('google_calendar_event_tags', 'google_calendar_events', 'google_calendars', 'google_oauth_states',
                  'google_calendar_connections'):
        op.drop_table(table)
