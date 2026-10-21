"""add tasks.position: the order of a task among its parent's linked tasks

Revision ID: a4669d8b3d50
Revises: 766e91c81956
Create Date: 2026-10-21 00:00:00.000000

Linked tasks (subtasks) are listed under their parent by (position, id).
Existing subtasks keep a NULL position, which sorts after numbered ones, so
they stay in creation order until their parent's links are next saved.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4669d8b3d50'
down_revision: Union[str, Sequence[str], None] = '766e91c81956'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('position', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('tasks', 'position')
