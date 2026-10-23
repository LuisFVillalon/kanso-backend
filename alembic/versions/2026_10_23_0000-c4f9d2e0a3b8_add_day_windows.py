"""add profiles.day_windows: a work window per weekday

Revision ID: c4f9d2e0a3b8
Revises: b3e8c1d9f2a7
Create Date: 2026-10-23 00:00:00.000000

  day_windows  JSON  nullable — {"0".."6": {"start": "HH:MM", "end": "HH:MM"}},
  weekdays numbered as rest_days (0=Sunday). A weekday without an entry, or a
  NULL column, uses day_start_time and shutoff_time as before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4f9d2e0a3b8'
down_revision: Union[str, Sequence[str], None] = 'b3e8c1d9f2a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('profiles', sa.Column('day_windows', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('profiles', 'day_windows')
