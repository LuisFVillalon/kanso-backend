"""add profiles.home_panels: the pair of home panels shown side by side

Revision ID: b7c1e2d3f4a5
Revises: a4669d8b3d50
Create Date: 2026-10-21 00:01:00.000000

  home_panels  VARCHAR  nullable — 'tasks,notes' | 'notes,canvas' | 'canvas,tasks'
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c1e2d3f4a5'
down_revision: Union[str, Sequence[str], None] = 'a4669d8b3d50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('profiles', sa.Column('home_panels', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('profiles', 'home_panels')
