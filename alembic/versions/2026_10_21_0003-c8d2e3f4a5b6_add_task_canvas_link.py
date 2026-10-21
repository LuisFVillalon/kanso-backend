"""add tasks.canvas_assignment_id and tasks.canvas_html_url: tasks added from Canvas

Revision ID: c8d2e3f4a5b6
Revises: c8d2f3e4a5b6
Create Date: 2026-10-21 00:03:00.000000

  canvas_assignment_id  BIGINT  nullable — the Canvas assignment the task follows
  canvas_html_url       TEXT    nullable — its page on the school's Canvas
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'c8d2f3e4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('canvas_assignment_id', sa.BigInteger(), nullable=True))
    op.add_column('tasks', sa.Column('canvas_html_url', sa.Text(), nullable=True))
    op.create_index(
        'ix_tasks_user_id_canvas_assignment_id',
        'tasks',
        ['user_id', 'canvas_assignment_id'],
        postgresql_where=sa.text('canvas_assignment_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_tasks_user_id_canvas_assignment_id', table_name='tasks')
    op.drop_column('tasks', 'canvas_html_url')
    op.drop_column('tasks', 'canvas_assignment_id')
