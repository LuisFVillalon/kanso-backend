"""add canvas auto-complete: the connection setting and the task's canvas_completed_at

Revision ID: a7c3e5f9b1d2
Revises: 239819931369
Create Date: 2026-10-23 00:03:00.000000

canvas_connections.auto_complete (default on) lets the linked-task sync
complete a task once its Canvas assignment is done. tasks.canvas_completed_at
records when the sync did, stored like completed_date, so the frontend can
show "Done in Canvas" and the sync doesn't complete a task the user reopened.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c3e5f9b1d2'
down_revision: Union[str, Sequence[str], None] = '239819931369'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'canvas_connections',
        sa.Column('auto_complete', sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column('tasks', sa.Column('canvas_completed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('tasks', 'canvas_completed_at')
    op.drop_column('canvas_connections', 'auto_complete')
