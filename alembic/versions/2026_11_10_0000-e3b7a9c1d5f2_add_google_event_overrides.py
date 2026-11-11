"""add google event overrides: kanso-only edits, archive, delete, busy and series changes

Revision ID: e3b7a9c1d5f2
Revises: c8d2f4a6b1e3
Create Date: 2026-11-10 00:00:00.000000

Imported Google events gain kanso-side state on their rows (edited_at for
kanso edits of their details, archive, trash, cancellation, busy and where
Google starts them), and google_event_overrides keeps "this and following"
and "all" changes to repeating Google events for the instances later syncs
import, with their tags in google_event_override_tags. Row Level Security is
enabled on the new tables, matching every other public table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3b7a9c1d5f2'
down_revision: Union[str, Sequence[str], None] = 'c8d2f4a6b1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'google_event_overrides',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'user_id',
            sa.String(length=36),
            sa.ForeignKey('google_calendar_connections.user_id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('recurring_event_id', sa.Text(), nullable=False),
        sa.Column('starts_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('changes', sa.JSON(), nullable=True),
        sa.Column('use_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        'ix_google_event_overrides_user_series', 'google_event_overrides', ['user_id', 'recurring_event_id']
    )

    op.create_table(
        'google_event_override_tags',
        sa.Column(
            'override_id',
            sa.Integer(),
            sa.ForeignKey('google_event_overrides.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column('tag_id', sa.Integer(), sa.ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
    )

    op.add_column('google_calendar_events', sa.Column('busy', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('google_calendar_events', sa.Column('google_starts_at', sa.DateTime(timezone=True), nullable=True))
    op.execute('UPDATE google_calendar_events SET google_starts_at = starts_at')
    op.alter_column('google_calendar_events', 'google_starts_at', nullable=False)
    op.add_column('google_calendar_events', sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('google_calendar_events', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'google_calendar_events',
        sa.Column(
            'archived_by_id',
            sa.Integer(),
            sa.ForeignKey('google_event_overrides.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )
    op.add_column('google_calendar_events', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'google_calendar_events',
        sa.Column(
            'deleted_by_id',
            sa.Integer(),
            sa.ForeignKey('google_event_overrides.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )
    op.add_column(
        'google_calendar_events', sa.Column('cancelled', sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        'google_calendar_events',
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        'ix_google_calendar_events_user_series',
        'google_calendar_events',
        ['user_id', 'recurring_event_id'],
        postgresql_where=sa.text('recurring_event_id IS NOT NULL'),
    )
    op.create_index(
        'ix_google_calendar_events_deleted_at',
        'google_calendar_events',
        ['deleted_at'],
        postgresql_where=sa.text('deleted_at IS NOT NULL'),
    )

    for table in ('google_event_overrides', 'google_event_override_tags'):
        op.execute(f'ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_index('ix_google_calendar_events_deleted_at', table_name='google_calendar_events')
    op.drop_index('ix_google_calendar_events_user_series', table_name='google_calendar_events')
    for column in (
        'created_at', 'cancelled', 'deleted_by_id', 'deleted_at', 'archived_by_id', 'archived_at', 'edited_at',
        'google_starts_at', 'busy',
    ):
        op.drop_column('google_calendar_events', column)
    op.drop_table('google_event_override_tags')
    op.drop_index('ix_google_event_overrides_user_series', table_name='google_event_overrides')
    op.drop_table('google_event_overrides')
