"""add Canvas calendar feed connections

Revision ID: e0f4a5b6c7d8
Revises: d9e3f4a5b6c7
Create Date: 2026-10-22 00:00:00.000000

A Canvas connection can now be a user's private calendar feed link instead
of an access token, for schools that don't allow tokens. The feed URL is a
secret and goes in the same encrypted token_* columns; a feed doesn't say
whose it is, so the Canvas account columns become nullable.

  canvas_connections.connection_type  VARCHAR(16)  NOT NULL DEFAULT 'token' — "token" or "feed"
  canvas_connections.canvas_user_id   now nullable — NULL for a feed
  canvas_connections.canvas_user_name now nullable — NULL for a feed
  canvas_connections.time_zone        TEXT nullable — the zone a feed's all-day items are due in
  tasks.canvas_due_date               DATE nullable — an all-day feed item's date as of the last sync
  tasks.canvas_due_source             VARCHAR(8) nullable — "token" or "feed", the source of the last sync

No table is added, so Row Level Security is unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e0f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'd9e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'canvas_connections',
        sa.Column('connection_type', sa.String(length=16), server_default='token', nullable=False),
    )
    op.alter_column('canvas_connections', 'canvas_user_id', existing_type=sa.BigInteger(), nullable=True)
    op.alter_column('canvas_connections', 'canvas_user_name', existing_type=sa.Text(), nullable=True)
    op.add_column('canvas_connections', sa.Column('time_zone', sa.Text(), nullable=True))
    op.add_column('tasks', sa.Column('canvas_due_date', sa.Date(), nullable=True))
    op.add_column('tasks', sa.Column('canvas_due_source', sa.String(length=8), nullable=True))


def downgrade() -> None:
    # Feed connections have no Canvas account, so they can't survive the NOT NULLs.
    op.execute("DELETE FROM canvas_connections WHERE connection_type = 'feed'")
    op.drop_column('tasks', 'canvas_due_source')
    op.drop_column('tasks', 'canvas_due_date')
    op.drop_column('canvas_connections', 'time_zone')
    op.alter_column('canvas_connections', 'canvas_user_name', existing_type=sa.Text(), nullable=False)
    op.alter_column('canvas_connections', 'canvas_user_id', existing_type=sa.BigInteger(), nullable=False)
    op.drop_column('canvas_connections', 'connection_type')
