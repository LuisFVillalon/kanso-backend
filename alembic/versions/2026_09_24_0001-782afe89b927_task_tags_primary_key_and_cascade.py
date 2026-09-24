"""give task_tags a primary key and ON DELETE CASCADE foreign keys

Revision ID: 782afe89b927
Revises: 79817780a595
Create Date: 2026-09-24 00:01:00.000000

task_tags predates Alembic (it was created by metadata.create_all), so unlike
note_tags/habit_tags it has nullable columns, no primary key (duplicate rows
were possible) and foreign keys without ON DELETE CASCADE (which forced
account deletion to clear it by hand). This brings it in line with the other
two join tables. Its original FK constraint names were never recorded, so
they are looked up from the catalog rather than assumed.
"""

from typing import Sequence, Union

from alembic import op

revision: str = '782afe89b927'
down_revision: Union[str, Sequence[str], None] = '79817780a595'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Rows that can't satisfy the new primary key: half-empty links, and all
    # but one copy of any duplicated (task_id, tag_id) pair.
    op.execute("DELETE FROM task_tags WHERE task_id IS NULL OR tag_id IS NULL")
    op.execute("""
        DELETE FROM task_tags a
        USING task_tags b
        WHERE a.ctid < b.ctid AND a.task_id = b.task_id AND a.tag_id = b.tag_id
    """)

    op.execute("""
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'public.task_tags'::regclass AND contype IN ('f', 'p')
            LOOP
                EXECUTE format('ALTER TABLE public.task_tags DROP CONSTRAINT %I', c.conname);
            END LOOP;
        END
        $$;
    """)

    op.create_primary_key("task_tags_pkey", "task_tags", ["task_id", "tag_id"])
    op.create_foreign_key(
        "task_tags_task_id_fkey", "task_tags", "tasks", ["task_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "task_tags_tag_id_fkey", "task_tags", "tags", ["tag_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint("task_tags_tag_id_fkey", "task_tags", type_="foreignkey")
    op.drop_constraint("task_tags_task_id_fkey", "task_tags", type_="foreignkey")
    op.drop_constraint("task_tags_pkey", "task_tags", type_="primary")
    op.create_foreign_key("task_tags_task_id_fkey", "task_tags", "tasks", ["task_id"], ["id"])
    op.create_foreign_key("task_tags_tag_id_fkey", "task_tags", "tags", ["tag_id"], ["id"])
    op.alter_column("task_tags", "task_id", nullable=True)
    op.alter_column("task_tags", "tag_id", nullable=True)
