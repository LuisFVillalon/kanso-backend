from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Time, func, text
from sqlalchemy.orm import relationship

from app.database.database import Base
from app.models.task_tag_model import task_tags


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # Lists default to active rows only, so index just those per user.
        Index("ix_tasks_user_id_active", "user_id", postgresql_where=text("archived_at IS NULL")),
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
    user_id = Column(String(36), nullable=True, index=True)
    # Set when the task is archived; NULL means active.
    archived_at = Column(DateTime(timezone=True), nullable=True)
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
