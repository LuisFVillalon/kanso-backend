"""add per-class default tags to Canvas connections

Revision ID: aaba43710aa6
Revises: dbe62a68ec2b
Create Date: 2026-10-22 00:03:00.000000

A user can pick the tags every task they add from a Canvas class gets. They
belong to the connection, like the courses hidden under "Courses to show",
and are reset when it moves to another Canvas account or school.

  canvas_connections.course_tag_ids  JSON NOT NULL DEFAULT '{}' — {"<canvas course id>": [tag id, ...]}

No table is added, so Row Level Security is unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'aaba43710aa6'
down_revision: Union[str, Sequence[str], None] = 'dbe62a68ec2b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'canvas_connections',
        sa.Column('course_tag_ids', sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('canvas_connections', 'course_tag_ids')
