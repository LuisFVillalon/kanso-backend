"""add canvas_connections: one encrypted Canvas LMS token per user

Revision ID: 89a87cdc6728
Revises: 766e91c81956
Create Date: 2026-10-20 00:02:00.000000

Each user can connect one Canvas account with a personal access token and
the school's Canvas address. The token is stored only as AES-256-GCM
ciphertext plus its nonce and key version; the key is a Fly secret
(CANVAS_TOKEN_KEY), never in the database. Unlike the dropped
google_calendar_tokens table, nothing here holds a credential in plaintext.
Removing the connection deletes the row outright; there is no trash.

Row Level Security is enabled, matching the lockdown of every other public
table; the lockdown migration's default privileges already keep the
anon/authenticated roles off it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '89a87cdc6728'
down_revision: Union[str, Sequence[str], None] = '766e91c81956'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'canvas_connections',
        sa.Column('user_id', sa.String(length=36), primary_key=True),
        sa.Column('base_url', sa.Text(), nullable=False),
        sa.Column('token_ciphertext', sa.LargeBinary(), nullable=False),
        sa.Column('token_nonce', sa.LargeBinary(), nullable=False),
        sa.Column('key_version', sa.SmallInteger(), nullable=False),
        sa.Column('token_last4', sa.String(length=4), nullable=False),
        sa.Column('canvas_user_id', sa.BigInteger(), nullable=False),
        sa.Column('canvas_user_name', sa.Text(), nullable=False),
        sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('hidden_course_ids', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.execute('ALTER TABLE public.canvas_connections ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table('canvas_connections')
