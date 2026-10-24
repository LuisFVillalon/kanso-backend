from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.orm import relationship

from app.database.database import Base
from app.models.task_tag_model import task_tags


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # Lists default to active, undeleted rows only, so index just those per user.
        Index(
            "ix_tasks_user_id_active", "user_id", postgresql_where=text("archived_at IS NULL AND deleted_at IS NULL")
        ),
        # The calendar activity range reads tasks by due date and by completion time.
        Index("ix_tasks_user_id_due_date_active", "user_id", "due_date", postgresql_where=text("archived_at IS NULL")),
        Index(
            "ix_tasks_user_id_completed_date_active",
            "user_id",
            "completed_date",
            postgresql_where=text("archived_at IS NULL"),
        ),
        # The expiry purge finds rows deleted past retention by deleted_at alone.
        Index("ix_tasks_deleted_at", "deleted_at", postgresql_where=text("deleted_at IS NOT NULL")),
        # Detaching subtasks of purged parents, and the FK check on deleting them.
        Index("ix_tasks_parent_task_id", "parent_task_id", postgresql_where=text("parent_task_id IS NOT NULL")),
        # The Canvas linked-task sync and the duplicate check read tasks by assignment.
        Index(
            "ix_tasks_user_id_canvas_assignment_id",
            "user_id",
            "canvas_assignment_id",
            postgresql_where=text("canvas_assignment_id IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    category = Column(String, nullable=True)
    completed = Column(Boolean, default=False)
    priority = Column(Integer, nullable=True, index=True)
    due_date = Column(DateTime, nullable=True)
    due_time = Column(Time, nullable=True)
    created_date = Column(DateTime, server_default=func.now(), nullable=False)
    completed_date = Column(DateTime, nullable=True)
    estimated_time = Column(Numeric(precision=10, scale=2), nullable=True)
    parent_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    # Order among the parent's linked tasks, listed by (position, id); NULL
    # for top-level tasks and for subtasks linked before ordering existed.
    position = Column(Integer, nullable=True)
    # Minutes before the due time to send the reminder notification; NULL
    # means the user's default (see notification_crud).
    reminder_minutes = Column(Integer, nullable=True)
    # The Canvas assignment a task added from the Canvas panel follows (see
    # crud/canvas_link_crud.py), and its page on the school's Canvas; NULL
    # for every other task. Canvas ids can exceed 32 bits.
    canvas_assignment_id = Column(BigInteger, nullable=True)
    canvas_html_url = Column(Text, nullable=True)
    # The assignment's due_at as of the last sync, so a sync moves the task's
    # due date only when Canvas's changed, not when the user edited it.
    canvas_due_at = Column(DateTime(timezone=True), nullable=True)
    # For an all-day calendar feed item, its date as of the last sync, which
    # the feed sync compares instead of canvas_due_at; NULL otherwise.
    canvas_due_date = Column(Date, nullable=True)
    # Which connection stored the two above, "token" or "feed"; NULL (read as
    # "token") for tasks linked before it was recorded. The first sync from the
    # other source only re-records them, since the two give the same due date
    # in different forms.
    canvas_due_source = Column(String(8), nullable=True)
    # When the linked-task sync completed the task because its Canvas item was
    # done, stored like completed_date (which it then equals); NULL otherwise.
    # Kept when the user reopens the task, so the sync doesn't complete it
    # again, and cleared when it's linked to another assignment.
    canvas_completed_at = Column(DateTime, nullable=True)
    user_id = Column(String(36), nullable=True, index=True)
    # Set when the task is archived; NULL means active.
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # Set when the task is deleted (moved to the trash); NULL otherwise.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    @property
    def completed_by_canvas(self) -> bool:
        """Completed by the linked-task sync, and not reopened or re-completed by the user since."""
        return (
            bool(self.completed)
            and self.canvas_completed_at is not None
            and self.completed_date == self.canvas_completed_at
        )

    tags = relationship(
        "Tag",
        secondary=task_tags,
        back_populates="tasks"
    )
    # Many-to-one link to the parent; the backref gives each task its subtasks.
    parent_task = relationship(
        "Task",
        remote_side=[id],
        backref="subtasks",
    )
